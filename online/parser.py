"""查询解析（在线组）。

骨架版只支持普通词项查询（默认 OR 候选，按相关度排序）。
布尔运算、引号短语、括号嵌套留给在线组实现——实现时先过
tests/ 里的黄金语法树用例（TODO: 20 条），并保证非法查询（如未闭合引号）
抛 QuerySyntaxError 而不是静默给出错误结果。
"""


class QuerySyntaxError(ValueError):
    """查询串不合法或使用了尚未支持的语法。server 层转成 HTTP 400。"""


_UNSUPPORTED = ('"', "'", "(", ")", "（", "）")
_OPERATORS = {"AND", "OR", "NOT"}


def parse(query: str) -> dict:
    """返回语法树的调试回显（/search 的 parsed 字段原样回传）。"""
    terms = query.split()
    if not terms:
        raise QuerySyntaxError("空查询")
    for t in terms:
        if t.upper() in _OPERATORS:
            raise QuerySyntaxError(f"布尔运算符 {t} 尚未实现（TODO: 在线组 parser）")
        if any(c in t for c in _UNSUPPORTED):
            raise QuerySyntaxError(f"短语/括号语法尚未实现（TODO: 在线组 parser）: {t!r}")
    return {"type": "terms", "terms": terms, "explicit_constraints": False}
