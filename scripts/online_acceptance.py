"""在线服务（RFC-0003）只读 HTTP 验收入口，只用标准库。

只调用公开 HTTP 面（``/health``、``/index/info``、``/search`` 和静态 UI），
不 import ``online.*`` / ``offline.*``，因此探针进程与服务进程不共享任何状态。
证据写入调用方指定的 JSON 路径；事件里的查询与请求体都来自调用方参数，
工具本身不读取、不写入任何凭据。
向量 profile 可用三个 ``--expect-embedding-*`` 参数绑定发布清单中的编码器身份。
四个 ``--expect-query-encoder-*`` 参数核验实际查询编码器，并启用同实例检查。

三个阶段的语义（同一个服务进程保持运行）：

* ``healthy``：向量后端可用。核对启动身份回显、四模式、分页、显式约束和错误矩阵。
* ``down``：启动后停掉向量后端。词法仍可用，dense/hybrid 返回 503，
  adaptive 显式回退词法；启动身份回显不受后端存活影响。
* ``recovered``：后端重启、服务进程不重启，语义模式恢复。

``--lexical-only`` 描述未部署 vector 块的词法 profile：它期望 503/回退，
而不是语义模式成功，并同样执行八类请求校验矩阵；四模式分页只对有活向量后端的
profile 有意义，词法 profile 不假设每个查询都有满页命中。``--query`` 必须是目标语料上
确实有词法命中的查询，``--oov-query`` 用于构造词法零命中的语义救援场景。每条检查只靠
``assert`` 判定，因此在启用优化（``python -O`` / ``PYTHONOPTIMIZE``）的解释器下直接
拒绝运行。``--require-instance-id`` 核对每个响应的服务实例身份，可把 healthy 收据中的
实例 ID 传给后续阶段的 ``--expect-instance-id``。
"""

import argparse
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

SCOPE = "公开 HTTP 工程验收；不是质量基准，也不是 HNSW 性能或容量结论"
INSTANCE_HEADER = "X-Search-Instance"
INSTANCE_ID = re.compile(r"[0-9a-f]{32}")
RUNTIME_LABEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}")
TIMINGS_KEYS = frozenset({"lexical", "router", "encode", "vector", "fusion"})
SCORE_KIND_BY_EXECUTED = {
    "lexical": ("bm25", "tfidf"),
    "dense": ("cosine",),
    "hybrid": ("rrf",),
}
VECTOR_USED_BY_MODE = {"lexical": False, "dense": True, "hybrid": True, "adaptive": None}
REQUIRED_VECTOR_EXPECTATIONS = (
    ("--expect-corpus-sha256", "expect_corpus_sha256"),
    ("--expect-collection", "expect_collection"),
    ("--expect-hnsw-ef", "expect_hnsw_ef"),
)


@dataclass(frozen=True)
class EmbeddingExpectation:
    model_id: str
    revision: str
    dimension: int


@dataclass(frozen=True)
class QueryEncoderRuntimeExpectation:
    implementation: str
    device: str
    dtype: str
    inventory_sha256: str


@dataclass(frozen=True)
class Expectation:
    """调用方声明的服务身份；缺失的期望不会被默默跳过。"""

    dataset: str
    n_docs: int
    corpus_sha256: str | None
    collection: str | None
    hnsw_ef: int | None
    candidate_k: int
    rrf_c: float
    policy_version: str | None
    vector_configured: bool
    embedding: EmbeddingExpectation | None = None
    query_encoder_runtime: QueryEncoderRuntimeExpectation | None = None

    def expected_vector_used(self, mode):
        return VECTOR_USED_BY_MODE[mode]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Probe:
    def __init__(self, endpoint, *, expectation, query, oov_query, run_tag, timeout,
                 require_instance_id=False, expect_instance_id=None):
        self.base = endpoint.rstrip("/")
        self.expectation = expectation
        self.query = query
        self.oov_query = oov_query
        self.run_tag = run_tag
        self.timeout = timeout
        self.expected_instance_id = expect_instance_id
        runtime = expectation.query_encoder_runtime
        self.expected_query_encoder_runtime = asdict(runtime) if runtime is not None else None
        self.require_instance_id = (require_instance_id or expect_instance_id is not None
                                    or runtime is not None)
        self.instance_id = None
        self.open = build_opener(NoRedirect()).open if self.require_instance_id else urlopen
        self.events = []
        self.checks = []

    def request(self, path, payload=None, expected=200):
        data = (None if payload is None
                else json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        request = Request(self.base + path, data=data,
                          headers={"Content-Type": "application/json"})
        started = time.monotonic()
        try:
            response = self.open(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
        except (URLError, OSError) as error:
            raise AssertionError(f"HTTP {path} 请求失败: {type(error).__name__}: {error}") from error
        with response:
            status = response.code
            if self.require_instance_id:
                if 300 <= status < 400:
                    raise AssertionError(f"HTTP {path} 重定向被拒绝（{status}）")
                instances = response.headers.get_all(INSTANCE_HEADER) or []
                if len(instances) != 1 or not INSTANCE_ID.fullmatch(instances[0]):
                    raise AssertionError(f"HTTP {path} 未返回唯一有效的 {INSTANCE_HEADER}")
                actual = instances[0]
                if self.expected_instance_id is not None and actual != self.expected_instance_id:
                    raise AssertionError(f"HTTP {path} 服务实例与期望不一致")
                if self.instance_id is not None and actual != self.instance_id:
                    raise AssertionError(f"HTTP {path} 服务实例在验收期间发生变化")
                self.instance_id = actual
            content_type = response.headers.get("Content-Type", "")
            raw = response.read()
        wall_ms = round((time.monotonic() - started) * 1000, 3)
        if "application/json" in content_type:
            try:
                body = json.loads(raw or b"null")
            except json.JSONDecodeError as error:
                raise AssertionError(f"HTTP {path} 返回非法 JSON: {error}") from error
        else:
            body = raw.decode("utf-8", "replace")[:2000]
        if path == "/index/info" and self.expected_query_encoder_runtime is not None:
            retrieval = body.get("retrieval") if isinstance(body, dict) else None
            runtime = retrieval.get("query_encoder_runtime") if isinstance(retrieval, dict) else None
            assert runtime == self.expected_query_encoder_runtime, (
                "query_encoder_runtime 缺失或与发布期望不一致"
            )
        self.events.append({"path": path, "request": payload, "status": status,
                            "wall_ms": wall_ms, "body": body})
        assert status == expected, f"HTTP {path} 状态 {status}，期望 {expected}: {body!r}"
        return body

    def search(self, query, mode="lexical", k=10, offset=0, expected=200, **options):
        payload = {"query": query, "mode": mode, "k": k, "offset": offset,
                   "run_tag": self.run_tag, **options}
        return self.request("/search", payload, expected)

    def check(self, name, fn):
        started = time.monotonic()
        try:
            fn()
        except Exception as error:  # noqa: BLE001 - 每条断言都转成证据，不中断后续检查
            detail = f"{type(error).__name__}: {error}"
            print(f"FAIL {name}: {detail}", flush=True)
            self.checks.append({"name": name, "status": "FAIL", "error": detail,
                                "took_ms": round((time.monotonic() - started) * 1000, 3)})
        else:
            print(f"PASS {name}", flush=True)
            self.checks.append({"name": name, "status": "PASS",
                                "took_ms": round((time.monotonic() - started) * 1000, 3)})


def _routing(probe, body, mode, *, offset=0, vector_used=None, fallback=False):
    """核对 HTTP v2.0 的固定结构、路由回显与可比较字段。"""
    expectation = probe.expectation
    assert body["api_version"] == "2.0", body.get("api_version")
    route = body["routing"]
    assert route["requested_mode"] == mode
    assert route["candidate_k"] == expectation.candidate_k
    version = route["policy_version"]
    assert isinstance(version, str) and version, "policy_version 必须是非空字符串"
    if expectation.policy_version is not None:
        assert version == expectation.policy_version, version
    assert route["fallback"] is fallback
    if vector_used is not None:
        assert route["vector_used"] is vector_used
        assert route["vector_attempted"] is (vector_used or fallback)
    expected_mode = (("dense" if mode == "dense" else "hybrid")
                     if route["vector_used"] else "lexical")
    assert route["executed_mode"] == expected_mode
    allowed_kinds = SCORE_KIND_BY_EXECUTED[route["executed_mode"]]
    assert route["score_kind"] in allowed_kinds, route["score_kind"]

    timings = body["timings_ms"]
    assert set(timings) == TIMINGS_KEYS, sorted(timings)
    assert all(type(value) in (int, float) and math.isfinite(value) and value >= 0
               for value in timings.values()), timings
    assert type(body["took_ms"]) is int and body["took_ms"] >= 0

    rows = body["results"]
    assert isinstance(rows, list)
    assert [row["rank"] for row in rows] == list(range(offset + 1, offset + 1 + len(rows)))
    doc_ids = [row["doc_id"] for row in rows]
    assert all(isinstance(doc_id, str) and doc_id for doc_id in doc_ids), doc_ids
    assert len(set(doc_ids)) == len(doc_ids), "分页结果内 doc_id 必须唯一"
    for row in rows:
        assert type(row["score"]) in (int, float) and math.isfinite(row["score"])
        assert isinstance(row["title"], str) and isinstance(row["snippet"], str)
        for hit in row["highlights"]:
            assert 0 <= hit["start"] < hit["end"], hit
    return rows


def _health(probe):
    body = probe.request("/health")
    assert body["status"] == "ok"
    index = body["index"]
    assert index["dataset"] == probe.expectation.dataset
    assert type(index["n_docs"]) is int and index["n_docs"] == probe.expectation.n_docs
    assert type(index["schema_version"]) is int
    assert isinstance(index["analyzer_hash"], str) and index["analyzer_hash"]


def _index_identity(probe):
    expectation = probe.expectation
    body = probe.request("/index/info")
    assert body["dataset"] == expectation.dataset
    assert type(body["n_docs"]) is int and body["n_docs"] == expectation.n_docs
    retrieval = body["retrieval"]
    # vector_ready 是启动时配置/身份对齐的结论，不是后端实时健康。
    assert retrieval["vector_ready"] is expectation.vector_configured

    identity = retrieval["index_identity"]
    assert identity["dataset"] == expectation.dataset
    assert type(identity["n_docs"]) is int and identity["n_docs"] == expectation.n_docs
    assert identity["schema_version"] == body["schema_version"]
    assert identity["analyzer_hash"] == body["analyzer"]["config_hash"]
    assert isinstance(identity["analyzer_hash"], str) and identity["analyzer_hash"]
    bm25 = identity["bm25"]
    # 产物没有声明 BM25 参数时（旧版/极简词法索引）只接受 null，不补造数值。
    assert bm25 is None or isinstance(bm25, dict), bm25
    if bm25 is not None:
        assert set(bm25) >= {"k1", "b", "idf"}, sorted(bm25)
        assert type(bm25["k1"]) in (int, float) and math.isfinite(bm25["k1"]) and bm25["k1"] > 0
        assert type(bm25["b"]) in (int, float) and math.isfinite(bm25["b"]) and 0 <= bm25["b"] <= 1
        assert isinstance(bm25["idf"], str) and bm25["idf"]

    assert retrieval["search_config"] == {"candidate_k": expectation.candidate_k,
                                          "rrf_c": expectation.rrf_c}
    assert retrieval["config"]["candidate_k"] == expectation.candidate_k
    assert retrieval["config"]["rrf_c"] == expectation.rrf_c
    policy = retrieval["policy"]
    assert isinstance(policy["name"], str) and policy["name"]
    assert isinstance(policy["policy_version"], str) and policy["policy_version"]
    assert isinstance(policy["requires_statistics"], bool)
    if expectation.policy_version is not None:
        assert policy["policy_version"] == expectation.policy_version

    if expectation.vector_configured:
        manifest = retrieval["vector_manifest"]
        assert isinstance(manifest, dict), "向量 profile 必须回显 vector_manifest"
        assert manifest["n_docs"] == expectation.n_docs
        assert manifest["corpus_sha256"] == expectation.corpus_sha256
        assert identity["corpus_sha256"] == expectation.corpus_sha256
    else:
        assert retrieval["vector_manifest"] is None
        assert retrieval["vector_identity"] is None


def _vector_identity(probe):
    expectation = probe.expectation
    retrieval = probe.request("/index/info")["retrieval"]
    manifest = retrieval["vector_manifest"]
    identity = retrieval["vector_identity"]
    assert isinstance(identity, dict), "向量 profile 必须回显 vector_identity"
    assert identity["collection"] == expectation.collection
    assert identity["n_docs"] == expectation.n_docs
    assert identity["corpus_sha256"] == expectation.corpus_sha256
    assert identity["backend"] == "qdrant-hnsw"
    for key, value in manifest.items():
        assert identity[key] == value, f"vector_identity.{key} 与 vector_manifest 不一致"
    embedding = identity["embedding"]
    assert isinstance(embedding["model_id"], str) and embedding["model_id"]
    revision = embedding["revision"]
    assert isinstance(revision, str) and revision and revision != "latest"
    assert type(embedding["dimension"]) is int and embedding["dimension"] > 0
    assert embedding["normalize"] is True and embedding["distance"] == "cosine"
    if expectation.embedding is not None:
        assert "embedding" in manifest, "vector_manifest 缺少 embedding"
        for key, expected in asdict(expectation.embedding).items():
            assert embedding[key] == expected, f"embedding.{key} 与发布期望不一致"
    search = identity["search"]
    assert search["hnsw_ef"] == expectation.hnsw_ef
    # exact=false 只说明允许 ANN，不证明本次查询实际走 HNSW。
    assert search["exact"] is False
    timeout_s = search["timeout_s"]
    assert type(timeout_s) in (int, float) and math.isfinite(timeout_s) and timeout_s > 0


def _ui_root(probe):
    body = probe.request("/")
    assert isinstance(body, str) and body.strip(), "静态 UI 根路径必须返回非空文本"


def _ui_traversal(probe):
    body = probe.request("/static/%2e%2e/README.md", expected=404)
    assert isinstance(body, dict) and "error" in body, "越界静态路径必须被拒绝"


def _mode(probe, mode):
    body = probe.search(probe.query, mode)
    expected = probe.expectation.expected_vector_used(mode)
    rows = _routing(probe, body, mode, vector_used=expected)
    assert rows, f"{mode} 模式在验收入参查询上返回空结果"


def _pagination(probe, mode):
    whole = _routing(probe, probe.search(probe.query, mode, k=10), mode,
                     vector_used=probe.expectation.expected_vector_used(mode))
    first = _routing(probe, probe.search(probe.query, mode, k=5), mode,
                     vector_used=probe.expectation.expected_vector_used(mode))
    second = _routing(probe, probe.search(probe.query, mode, k=5, offset=5), mode, offset=5,
                      vector_used=probe.expectation.expected_vector_used(mode))
    assert len(whole) == 10, len(whole)
    assert len(first) == 5 and len(second) == 5
    assert [(row["doc_id"], row["rank"]) for row in whole] == [
        (row["doc_id"], row["rank"]) for row in first + second]
    assert len({row["doc_id"] for row in whole}) == 10


def _oov_rescue(probe):
    lexical = probe.search(probe.oov_query, "lexical")
    assert _routing(probe, lexical, "lexical", vector_used=False) == [], "OOV 查询必须词法零命中"
    adaptive = probe.search(probe.oov_query, "adaptive")
    rows = _routing(probe, adaptive, "adaptive", vector_used=True)
    assert rows, "adaptive 必须在词法零命中时触发语义召回"


def _explicit_constraints(probe):
    query = f'"{probe.oov_query}"'
    probe.search(query, "dense", expected=400)
    lexical = probe.search(query, "lexical")
    assert _routing(probe, lexical, "lexical", vector_used=False) == []
    hybrid = probe.search(query, "hybrid")
    assert _routing(probe, hybrid, "hybrid", vector_used=True) == []
    adaptive = probe.search(query, "adaptive")
    assert _routing(probe, adaptive, "adaptive", vector_used=False) == []
    assert adaptive["routing"]["reason"] == "explicit_constraints"


def _error_matrix(probe):
    expectation = probe.expectation
    cases = (
        ("empty_query", {"query": " "}),
        ("unknown_field", {"query": probe.query, "unknown_option": 1}),
        ("bad_k", {"query": probe.query, "k": 201}),
        ("bad_offset", {"query": probe.query, "offset": -1}),
        ("bool_k", {"query": probe.query, "k": True}),
        ("candidate_boundary", {"query": probe.query, "mode": "dense",
                                "offset": expectation.candidate_k - 1, "k": 2}),
        ("incompatible_scorer", {"query": probe.query, "mode": "hybrid",
                                 "scorer": "tfidf"}),
        ("syntax_error", {"query": '"unterminated'}),
    )
    for name, payload in cases:
        probe.check(name, lambda payload=payload: probe.request("/search", payload,
                                                                expected=400))


def _vector_modes(probe, prefix, *, rows_required=True):
    for mode in ("lexical", "dense", "hybrid", "adaptive"):
        def check(mode=mode):
            body = probe.search(probe.query, mode)
            rows = _routing(probe, body, mode,
                            vector_used=probe.expectation.expected_vector_used(mode))
            if rows_required:
                assert rows, f"{mode} 模式返回空结果"
        probe.check(f"{prefix}_{mode}", check)


def _down_checks(probe):
    def lexical_survives():
        rows = _routing(probe, probe.search(probe.query, "lexical"), "lexical",
                        vector_used=False)
        assert rows, "后端不可用时词法模式仍必须返回结果"
    probe.check("lexical_serves_without_vector", lexical_survives)

    for mode in ("dense", "hybrid"):
        def unavailable(mode=mode):
            body = probe.search(probe.query, mode, expected=503)
            assert body.get("code") == "vector_unavailable", body
        probe.check(f"{mode}_unavailable_503", unavailable)

    def adaptive_falls_back():
        body = probe.search(probe.oov_query, "adaptive")
        rows = _routing(probe, body, "adaptive", vector_used=False, fallback=True)
        assert body["routing"]["reason"] == "vector_unavailable", body["routing"]["reason"]
        assert rows == []
    probe.check("adaptive_explicit_fallback", adaptive_falls_back)

    probe.check("dense_constraints_still_400",
                lambda: probe.search(f'"{probe.oov_query}"', "dense", expected=400))


def _recovered_checks(probe):
    def vector_mode(mode, query):
        body = probe.search(query, mode)
        rows = _routing(probe, body, mode, vector_used=True)
        assert rows, f"{mode} 模式未在后端恢复后返回结果"

    probe.check("recovered_dense", lambda: vector_mode("dense", probe.query))
    probe.check("recovered_hybrid", lambda: vector_mode("hybrid", probe.query))
    probe.check("recovered_adaptive",
                lambda: vector_mode("adaptive", probe.oov_query))
    probe.check("recovered_lexical",
                lambda: _routing(probe, probe.search(probe.query, "lexical"), "lexical",
                                 vector_used=False))


def run_checks(probe, phase, lexical_only):
    probe.check("health_is_lexical_liveness", lambda: _health(probe))
    probe.check("index_identity", lambda: _index_identity(probe))
    if not lexical_only:
        probe.check("vector_identity", lambda: _vector_identity(probe))
    probe.check("ui_root", lambda: _ui_root(probe))
    probe.check("static_traversal_blocked", lambda: _ui_traversal(probe))

    vector_live = not lexical_only and phase != "down"
    if vector_live:
        _vector_modes(probe, "mode")
        for mode in ("lexical", "dense", "hybrid", "adaptive"):
            probe.check(f"pagination_{mode}", lambda mode=mode: _pagination(probe, mode))
        probe.check("oov_semantic_rescue", lambda: _oov_rescue(probe))
        probe.check("explicit_constraints", lambda: _explicit_constraints(probe))
        _error_matrix(probe)
        if phase == "recovered":
            _recovered_checks(probe)
    else:
        _down_checks(probe)
        if lexical_only:
            # 请求校验矩阵与向量后端无关：词法 profile 也必须满足冻结的请求契约。
            _error_matrix(probe)


def _sha256_digest(value):
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise argparse.ArgumentTypeError("必须是 64 位小写 SHA256 十六进制摘要")
    return value


def _positive_int(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是正整数") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def _positive_float(value):
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是有限正数") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("必须是有限正数")
    return number


def _instance_id(value):
    if not INSTANCE_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("必须是 32 位小写十六进制服务实例 ID")
    return value


def _identity_string(value):
    if not value or value != value.strip():
        raise argparse.ArgumentTypeError("必须是非空字符串且首尾无空白")
    return value


def _fixed_revision(value):
    revision = _identity_string(value)
    if revision.casefold() == "latest":
        raise argparse.ArgumentTypeError("必须是发布清单中的固定 revision")
    return revision


def _runtime_label(value):
    if not RUNTIME_LABEL.fullmatch(value):
        raise argparse.ArgumentTypeError("必须是 1 至 64 位查询编码器运行时标识")
    return value


def build_parser():
    parser = argparse.ArgumentParser(
        description="在线服务只读 HTTP 验收（RFC-0003）：healthy / down / recovered 三阶段")
    parser.add_argument("--endpoint", required=True, help="服务基址，例如 http://127.0.0.1:8000")
    parser.add_argument("--out", required=True, type=Path, help="证据 JSON 输出路径")
    parser.add_argument("--phase", choices=("healthy", "down", "recovered"), default="healthy")
    parser.add_argument("--expect-dataset", required=True, help="与 manifest 完全一致的 dataset")
    parser.add_argument("--expect-n-docs", required=True, type=_positive_int)
    parser.add_argument("--expect-corpus-sha256", type=_sha256_digest,
                        help="向量 profile 必填：预期语料指纹")
    parser.add_argument("--expect-collection", help="向量 profile 必填：Qdrant collection")
    parser.add_argument("--expect-hnsw-ef", type=_positive_int,
                        help="向量 profile 必填：服务声明的搜索 beam")
    parser.add_argument("--expect-embedding-model-id", type=_identity_string,
                        help="发布清单中的编码器型号；三个编码器参数须同时提供")
    parser.add_argument("--expect-embedding-revision", type=_fixed_revision,
                        help="发布清单中的固定编码器 revision")
    parser.add_argument("--expect-embedding-dimension", type=_positive_int,
                        help="发布清单中的编码维度")
    parser.add_argument("--expect-query-encoder-implementation", type=_runtime_label,
                        help="获准发布配置的编码器实现；四个运行时参数须同时提供")
    parser.add_argument("--expect-query-encoder-device", type=_runtime_label,
                        help="获准发布配置的查询编码器设备")
    parser.add_argument("--expect-query-encoder-dtype", type=_runtime_label,
                        help="获准发布配置的查询编码器权重精度")
    parser.add_argument("--expect-query-encoder-inventory-sha256", type=_sha256_digest,
                        help="获准发布模型的清单摘要")
    parser.add_argument("--expect-candidate-k", type=_positive_int, default=200)
    parser.add_argument("--expect-rrf-c", type=_positive_float, default=60.0)
    parser.add_argument("--expect-policy-version", help="可选：要求回显的 policy_version")
    parser.add_argument("--require-instance-id", action="store_true",
                        help="要求所有响应来自同一服务实例并拒绝重定向")
    parser.add_argument("--expect-instance-id", type=_instance_id,
                        help="要求所有响应来自给定服务实例；自动启用实例校验")
    parser.add_argument("--lexical-only", action="store_true",
                        help="未部署 vector 块的词法 profile；期望 503 与显式回退")
    parser.add_argument("--query", required=True, help="目标语料上确实有词法命中的查询")
    parser.add_argument("--oov-query", default="onlineacceptancezzqx2026",
                        help="目标语料上零词法命中的探针专用词串")
    parser.add_argument("--run-tag", default="online-acceptance",
                        help="随每个请求发送的 run_tag，用于与服务日志对账")
    parser.add_argument("--timeout", type=_positive_float, default=30.0,
                        help="单个 HTTP 请求的 socket 超时秒数；应大于服务 request_timeout_s")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not __debug__:
        # 断言是每条检查的唯一判据：-O/PYTHONOPTIMIZE 会把失败变成静默 PASS。
        parser.error("必须在未启用优化（python -O / PYTHONOPTIMIZE）的解释器下运行")
    if args.lexical_only and args.phase == "recovered":
        parser.error("--lexical-only 与 --phase recovered 矛盾：词法 profile 没有可恢复的语义后端")
    if not args.lexical_only:
        missing = [flag for flag, attr in REQUIRED_VECTOR_EXPECTATIONS
                   if getattr(args, attr) is None]
        if missing:
            parser.error("向量 profile 需要同时给出预期身份: " + ", ".join(missing))

    embedding_values = (args.expect_embedding_model_id, args.expect_embedding_revision,
                        args.expect_embedding_dimension)
    embedding = None
    if any(value is not None for value in embedding_values):
        if args.lexical_only:
            parser.error("--lexical-only 不接受编码器身份参数")
        if any(value is None for value in embedding_values):
            parser.error("必须同时给出 --expect-embedding-model-id、"
                         "--expect-embedding-revision 和 --expect-embedding-dimension")
        embedding = EmbeddingExpectation(*embedding_values)

    runtime_values = (args.expect_query_encoder_implementation, args.expect_query_encoder_device,
                      args.expect_query_encoder_dtype, args.expect_query_encoder_inventory_sha256)
    runtime = None
    if any(value is not None for value in runtime_values):
        if args.lexical_only:
            parser.error("--lexical-only 不接受查询编码器运行时参数")
        if any(value is None for value in runtime_values):
            parser.error("必须同时给出四个 --expect-query-encoder-* 参数")
        runtime = QueryEncoderRuntimeExpectation(*runtime_values)

    expectation = Expectation(
        dataset=args.expect_dataset,
        n_docs=args.expect_n_docs,
        corpus_sha256=args.expect_corpus_sha256,
        collection=args.expect_collection,
        hnsw_ef=args.expect_hnsw_ef,
        candidate_k=args.expect_candidate_k,
        rrf_c=args.expect_rrf_c,
        policy_version=args.expect_policy_version,
        vector_configured=not args.lexical_only,
        embedding=embedding,
        query_encoder_runtime=runtime,
    )
    probe = Probe(args.endpoint, expectation=expectation, query=args.query,
                  oov_query=args.oov_query, run_tag=args.run_tag, timeout=args.timeout,
                  require_instance_id=args.require_instance_id,
                  expect_instance_id=args.expect_instance_id)
    started_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        run_checks(probe, args.phase, args.lexical_only)
    except Exception as error:  # noqa: BLE001 - 探针自身错误也要留下证据
        detail = f"{type(error).__name__}: {error}"
        print(f"FAIL probe_crashed: {detail}", flush=True)
        probe.checks.append({"name": "probe_crashed", "status": "FAIL", "error": detail})

    failures = [check for check in probe.checks if check["status"] == "FAIL"]
    expected_identity = asdict(expectation)
    if embedding is None:
        expected_identity.pop("embedding")
    if runtime is None:
        expected_identity.pop("query_encoder_runtime")
    evidence = {
        "status": "FAIL" if failures else "PASS",
        "endpoint": probe.base,
        "phase": args.phase,
        "lexical_only": args.lexical_only,
        "run_tag": args.run_tag,
        "started_utc": started_utc,
        "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "expectation": expected_identity,
        "scope": SCOPE,
        "checks_total": len(probe.checks),
        "checks_passed": len(probe.checks) - len(failures),
        "failures": [check["name"] for check in failures],
        "checks": probe.checks,
        "events": probe.events,
    }
    if probe.require_instance_id:
        evidence["instance_binding"] = {
            "header": INSTANCE_HEADER,
            "expected_id": probe.expected_instance_id,
            "verified_id": probe.instance_id if not failures else None,
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"{evidence['checks_passed']}/{evidence['checks_total']} 通过；"
          f"阶段 {evidence['phase']}；证据 {args.out}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
