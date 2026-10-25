"""Query parser for natural-language, boolean, and phrase queries.

Whitespace between ordinary atoms keeps the historical OR semantics. Explicit
``AND``, ``OR``, ``NOT``, parentheses, and double-quoted phrases form a small
boolean grammar with the usual precedence (NOT, AND, OR).
"""

from __future__ import annotations

from dataclasses import dataclass


class QuerySyntaxError(ValueError):
    """The query is malformed or contains an unsupported construct."""


_OPERATORS = {"AND", "OR", "NOT"}


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    pos: int


def _lex(query: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    while i < len(query):
        if query[i].isspace():
            i += 1
            continue
        if query[i] == "(":
            # Natural-language queries sometimes use ``( )`` as punctuation,
            # especially in fill-in-the-blank questions. An empty pair has
            # no boolean meaning, so ignore it; non-empty parentheses remain
            # grammar tokens and are still validated strictly below.
            closing = i + 1
            while closing < len(query) and query[closing].isspace():
                closing += 1
            if closing < len(query) and query[closing] == ")":
                i = closing + 1
                continue
            tokens.append(_Token("(", "(", i))
            i += 1
            continue
        if query[i] == ")":
            tokens.append(_Token(query[i], query[i], i))
            i += 1
            continue
        if query[i] == '"':
            start = i
            i += 1
            chars: list[str] = []
            closed = False
            while i < len(query):
                char = query[i]
                if char == "\\":
                    if i + 1 >= len(query):
                        raise QuerySyntaxError("短语中的转义符没有后继字符")
                    chars.append(query[i + 1])
                    i += 2
                elif char == '"':
                    closed = True
                    i += 1
                    break
                else:
                    chars.append(char)
                    i += 1
            if not closed:
                raise QuerySyntaxError(f"未闭合短语（位置 {start}）")
            value = "".join(chars).strip()
            if not value:
                raise QuerySyntaxError("短语不能为空")
            tokens.append(_Token("PHRASE", value, start))
            continue
        start = i
        while i < len(query) and not query[i].isspace() and query[i] not in '()"':
            i += 1
        value = query[start:i]
        if not value:
            raise QuerySyntaxError(f"无法解析查询（位置 {i}）")
        upper = value.upper()
        tokens.append(_Token(upper if upper in _OPERATORS else "TERM", value, start))
    if not tokens:
        raise QuerySyntaxError("空查询")
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Token]):
        self.tokens = tokens
        self.i = 0

    def peek(self) -> _Token | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> _Token:
        token = self.peek()
        if token is None:
            raise QuerySyntaxError("查询在此处意外结束")
        self.i += 1
        return token

    @staticmethod
    def _starts_unary(token: _Token | None) -> bool:
        return token is not None and token.kind in {"TERM", "PHRASE", "(", "NOT"}

    def parse(self) -> dict:
        node = self.parse_or()
        if self.peek() is not None:
            token = self.peek()
            if token.kind == ")":
                raise QuerySyntaxError(f"多余的右括号（位置 {token.pos}）")
            raise QuerySyntaxError(f"运算符 {token.value!r} 缺少左操作数（位置 {token.pos}）")
        return node

    def parse_or(self) -> dict:
        children = [self.parse_and()]
        while True:
            token = self.peek()
            if token is not None and token.kind == "OR":
                self.take()
                children.append(self.parse_and())
            elif self._starts_unary(token):
                # Ordinary whitespace queries mean OR, preserving v2 behavior.
                children.append(self.parse_and())
            else:
                break
        return children[0] if len(children) == 1 else {"op": "or", "children": children}

    def parse_and(self) -> dict:
        children = [self.parse_unary()]
        while True:
            token = self.peek()
            if token is None:
                break
            if token.kind == "AND":
                self.take()
                children.append(self.parse_unary())
                continue
            # Search syntax commonly writes ``a NOT b`` as an implicit
            # conjunction. Treating NOT here also keeps explicit exclusion
            # from becoming a broad OR query.
            if token.kind == "NOT":
                children.append(self.parse_unary())
                continue
            break
        return children[0] if len(children) == 1 else {"op": "and", "children": children}

    def parse_unary(self) -> dict:
        token = self.peek()
        if token is not None and token.kind == "NOT":
            self.take()
            return {"op": "not", "child": self.parse_unary()}
        return self.parse_primary()

    def parse_primary(self) -> dict:
        token = self.take()
        if token.kind in {"TERM", "PHRASE"}:
            return {"op": "phrase" if token.kind == "PHRASE" else "term", "value": token.value}
        if token.kind == "(":
            node = self.parse_or()
            closing = self.peek()
            if closing is None or closing.kind != ")":
                raise QuerySyntaxError(f"缺少右括号（位置 {token.pos}）")
            self.take()
            return node
        if token.kind == ")":
            raise QuerySyntaxError(f"多余的右括号（位置 {token.pos}）")
        raise QuerySyntaxError(f"运算符 {token.value!r} 缺少操作数（位置 {token.pos}）")


def _atoms(node: dict, negated: bool = False) -> list[str]:
    """Collect positive scoring atoms; negative atoms remain in the filter AST."""
    op = node["op"]
    if op in {"term", "phrase"}:
        return [] if negated else [node["value"]]
    if op == "not":
        return _atoms(node["child"], not negated)
    return [value for child in node["children"] for value in _atoms(child, negated)]


def parse(query: str) -> dict:
    """Return a JSON-safe query AST and its positive scoring strings."""
    if not isinstance(query, str) or not query.strip():
        raise QuerySyntaxError("空查询")
    tokens = _lex(query)
    ast = _Parser(tokens).parse()
    atoms = _atoms(ast)
    has_explicit_syntax = any(token.kind in _OPERATORS or token.kind in {"(", ")", "PHRASE"}
                              for token in tokens)
    if not has_explicit_syntax and ast["op"] == "term" and len(atoms) == 1:
        return {"type": "terms", "terms": atoms, "explicit_constraints": False, "ast": ast}
    if not has_explicit_syntax and ast["op"] == "or" and all(
            child["op"] == "term" for child in ast["children"]):
        return {"type": "terms", "terms": atoms, "explicit_constraints": False, "ast": ast}
    return {"type": "boolean", "terms": atoms, "explicit_constraints": True, "ast": ast}
