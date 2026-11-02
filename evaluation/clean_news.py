"""Frozen news.cn extraction and text deduplication rules (no relevance inputs).

``raw_html_utf8_sha256`` hashes the caller's decoded HTML re-encoded as UTF-8;
it is NOT a hash of original HTTP response bytes. Extraction never falls back
from an unknown template to whole-page text. Exact dedup normalization affects
only the key, not the persisted article body.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
from html.parser import HTMLParser
import re
import unicodedata
from urllib.parse import urlsplit

from contracts.analyzer import analyze, config_hash
from evaluation.crawl_web import CrawlError, normalize_url

RULE_VERSION = "news-cn-body-v1"
MIN_BODY_LENGTH = 80
MIN_CJK_COUNT = 40
MAX_REPLACEMENT_RATIO = 0.01
NEAR_HAMMING_MAX = 3
NEAR_LENGTH_RATIO_MIN = 0.9
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "nav", "button", "form"})
_SKIP_MARKERS = frozenset({"articleedit", "editor", "relatednews", "nextpage", "share", "fontsize", "advise", "pagevideo"})
_BLOCK_TAGS = frozenset({"p", "div", "section", "article", "br", "li", "h1", "h2", "h3", "blockquote", "tr"})
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]")
# Match complete UI/error titles, never keywords appearing in a news story.
_TEMPLATE_TITLES = re.compile(r"^(?:404(?:\s*[-:：]?\s*(?:not found|页面不存在|页面未找到))?|页面不存在|页面未找到|访问出错|访问验证|安全验证|请输入验证码|验证码验证|用户登录|用户登陆|登录|登陆|sign in|log in|access denied|not found)$", re.I)


@dataclass(frozen=True)
class ArticleMetadata:
    rule_version: str
    source_url: str | None
    body_selector: str | None
    raw_html_utf8_sha256: str
    cleaned_body_sha256: str
    normalized_text_sha256: str
    published_at: str | None
    filter_reasons: tuple[str, ...]
    analyzer_config_hash: str

    @property
    def accepted(self) -> bool:
        return not self.filter_reasons


@dataclass(frozen=True)
class Article:
    title: str
    text: str
    metadata: ArticleMetadata

    @property
    def accepted(self) -> bool:
        return self.metadata.accepted


@dataclass(frozen=True)
class TextSignature:
    normalized_text_sha256: str
    simhash64: int
    length: int


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list["_Node | str"] = field(default_factory=list)
    excluded_ancestor: bool = False


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]
        self.nodes: list[_Node] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # HTML permits omitted </p>; prevent the next paragraph nesting in it.
        if tag == "p" and self.stack[-1].tag == "p":
            self.stack.pop()
        node = _Node(tag, {k: v or "" for k, v in attrs})
        node.excluded_ancestor = self.stack[-1].excluded_ancestor or _excluded(self.stack[-1])
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _excluded(node: _Node) -> bool:
    markers = {node.attrs.get("id", "").lower(), *node.attrs.get("class", "").lower().split()}
    return node.tag in _SKIP_TAGS or bool(markers & _SKIP_MARKERS) or "hidden" in node.attrs or bool(re.search(r"display\s*:\s*none", node.attrs.get("style", ""), re.I))


def _text(node: _Node) -> str:
    parts: list[str] = []

    def walk(current: _Node) -> None:
        if _excluded(current):
            return
        if current.tag in _BLOCK_TAGS:
            parts.append("\n")
        for child in current.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                walk(child)
        if current.tag in _BLOCK_TAGS:
            parts.append("\n")

    walk(node)
    lines = (re.sub(r"\s+", " ", line).strip() for line in "".join(parts).splitlines())
    return "\n".join(line for line in lines if line)


def normalize_text(body: str) -> str:
    """Exact-dedup key normalization only: NFKC and collapsed whitespace."""
    return " ".join(unicodedata.normalize("NFKC", body).split())


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def simhash64(body: str) -> int:
    """Frequency-weighted shared-analyzer terms; SHA256 first 64 bits, big endian.

    Zero-weight ties map to zero. No terms maps to zero. Near-duplicate comparison
    rejects empty normalized text, so empty values never become near duplicates.
    """
    frequencies = Counter(token.term for token in analyze(normalize_text(body)))
    weights = [0] * 64
    for term, count in frequencies.items():
        value = int.from_bytes(hashlib.sha256(term.encode("utf-8")).digest()[:8], "big")
        for bit in range(64):
            weights[bit] += count if value & (1 << bit) else -count
    return sum(1 << bit for bit, weight in enumerate(weights) if weight > 0)


def signature(body: str) -> TextSignature:
    normalized = normalize_text(body)
    return TextSignature(_sha256(normalized), simhash64(body), len(re.sub(r"\s", "", normalized)))


def is_near_duplicate(left: TextSignature, right: TextSignature) -> bool:
    """Frozen Hamming <= 3 and non-whitespace normalized length ratio >= .9."""
    if not left.length or not right.length:
        return False
    return ((left.simhash64 ^ right.simhash64).bit_count() <= NEAR_HAMMING_MAX
            and min(left.length, right.length) / max(left.length, right.length) >= NEAR_LENGTH_RATIO_MIN)


near_duplicate = is_near_duplicate


def extract_article(html: str, url: str) -> Article:
    """Extract recognized news.cn article HTML; return explicit rejection reasons."""
    reasons: list[str] = []
    source_url = None
    try:
        normalized_url = normalize_url(url)
        parts = urlsplit(normalized_url)
        if parts.netloc != "www.news.cn" or not parts.path.endswith("/c.html"):
            reasons.append("unsupported_article_url")
        else:
            source_url = normalized_url
    except (CrawlError, ValueError):
        reasons.append("invalid_article_url")
    parser = _TreeParser()
    parser.feed(html)
    parser.close()
    eligible_nodes = [node for node in parser.nodes if not node.excluded_ancestor and not _excluded(node)]
    title_node = next((node for node in eligible_nodes if node.tag == "title"), None)
    title = _text(title_node) if title_node else ""
    title = re.sub(r"\s*[-_—|]\s*新华网\s*$", "", title).strip()
    candidates = [node for node in eligible_nodes if node.attrs.get("id") == "detailContent"]
    selector = "#detailContent" if candidates else None
    if not candidates:
        candidates = [node for node in eligible_nodes if node.attrs.get("id") == "detail"]
        if candidates:
            selector = "#detail"
    body = _text(candidates[0]) if candidates else ""
    if not candidates:
        reasons.append("unrecognized_body_template")
    if not title:
        reasons.append("empty_title")
    if _TEMPLATE_TITLES.fullmatch(title):
        reasons.append("error_or_login_template")
    length = len(re.sub(r"\s", "", body))
    if length < MIN_BODY_LENGTH:
        reasons.append("body_too_short")
    if len(_CJK.findall(body)) < MIN_CJK_COUNT:
        reasons.append("insufficient_cjk")
    if body.count("\ufffd") / max(length, 1) > MAX_REPLACEMENT_RATIO:
        reasons.append("replacement_character_ratio")
    published_at = None
    for node in parser.nodes:
        if node.tag == "meta" and (node.attrs.get("name", "").lower() in {"publishdate", "pubdate", "publish_date"}
                                   or node.attrs.get("property", "").lower() == "article:published_time"):
            value = node.attrs.get("content", "").strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?", value):
                published_at = value
                break
    metadata = ArticleMetadata(RULE_VERSION, source_url, selector, _sha256(html), _sha256(body),
                               _sha256(normalize_text(body)), published_at, tuple(reasons), config_hash())
    return Article(title, body, metadata)
