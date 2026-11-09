"""``scripts/online_acceptance.py`` 的假服务回归测试。

假服务是独立的 ``ThreadingHTTPServer``，只模仿已冻结的公开 HTTP 形状；
它不导入 ``online.*``/``offline.*``，也不需要真实 Qdrant 或模型。测试覆盖
happy path、身份不匹配、后端不可用/恢复、词法 profile、错误矩阵缺失校验、
静态路径泄漏和完全不可达端点，避免只验证快照式成功。
"""

import ast
import contextlib
import io
import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import online_acceptance as acceptance  # noqa: E402

DATASET = "fake-news-20k"
N_DOCS = 20000
CORPUS_SHA256 = "1480ab10" + "0" * 56
COLLECTION = "fake_news_collection"
HNSW_EF = 256
QUERY = "心脏 检查"
OOV_TOKEN = "zzqx"
ALL_DOC_IDS = [f"fake-doc-{index:02d}" for index in range(30)]
ALLOWED_FIELDS = frozenset({"query", "mode", "k", "offset", "scorer", "expander", "run_tag"})
MODES = ("lexical", "dense", "hybrid", "adaptive")


class BadRequest(ValueError):
    pass


class FakeState:
    """可配置的假服务状态；默认是健康的向量 profile。"""

    def __init__(self, *, dataset=DATASET, n_docs=N_DOCS, corpus_sha256=CORPUS_SHA256,
                 collection=COLLECTION, hnsw_ef=HNSW_EF, candidate_k=200, rrf_c=60.0,
                 policy_version="rules-v1", vector_configured=True, backend_up=True,
                 lexical_up=True, adaptive_falls_back=True, unavailable_status=503,
                 accept_invalid_requests=False, serve_traversal=False,
                 identity_dataset=None, identity_n_docs=None, identity_corpus_sha256=None,
                 identity_analyzer_hash="fake-analyzer-hash"):
        self.dataset = dataset
        self.n_docs = n_docs
        self.corpus_sha256 = corpus_sha256
        self.collection = collection
        self.hnsw_ef = hnsw_ef
        self.candidate_k = candidate_k
        self.rrf_c = rrf_c
        self.policy_version = policy_version
        self.vector_configured = vector_configured
        self.backend_up = backend_up
        self.lexical_up = lexical_up
        self.adaptive_falls_back = adaptive_falls_back
        self.unavailable_status = unavailable_status
        self.accept_invalid_requests = accept_invalid_requests
        self.serve_traversal = serve_traversal
        self.identity_dataset = dataset if identity_dataset is None else identity_dataset
        self.identity_n_docs = n_docs if identity_n_docs is None else identity_n_docs
        self.identity_corpus_sha256 = (corpus_sha256 if identity_corpus_sha256 is None
                                       else identity_corpus_sha256)
        self.analyzer_hash = identity_analyzer_hash

    def manifest(self):
        return {
            "schema_version": 1,
            "dataset": self.dataset,
            "n_docs": self.n_docs,
            "analyzer": {"name": "bigram", "config_hash": self.analyzer_hash},
            "bm25": {"k1": 1.2, "b": 0.75, "idf": "log1p-lucene"},
        }

    def vector_manifest(self):
        if not self.vector_configured:
            return None
        return {
            "schema_version": 1,
            "corpus_sha256": self.corpus_sha256,
            "n_docs": self.n_docs,
            "embedding": {
                "model_id": "fake-pinned-embedder", "revision": "fake-revision-1",
                "dimension": 4, "max_tokens": 512, "normalize": True, "distance": "cosine",
                "document_template": "title\ntext", "query_prefix": "", "document_prefix": "",
            },
            "collection": self.collection,
            "backend": "qdrant-hnsw",
            "index_params": {"m": 16, "ef_construct": 100},
            "snapshot_files": {"fake.snapshot": "e" * 64},
        }

    def info(self):
        manifest = self.vector_manifest()
        vector_identity = None
        if manifest is not None:
            vector_identity = dict(manifest, search={
                "hnsw_ef": self.hnsw_ef, "exact": False, "timeout_s": 30.0,
            })
        return {
            "vector_ready": self.vector_configured,
            "config": {"candidate_k": self.candidate_k, "rrf_c": self.rrf_c,
                       "min_vocab_coverage": 0.7, "min_relative_gap": 0.1,
                       "policy_version": self.policy_version},
            "vector_manifest": manifest,
            "index_identity": {
                "dataset": self.identity_dataset,
                "n_docs": self.identity_n_docs,
                "schema_version": 1,
                "analyzer_hash": self.analyzer_hash,
                "corpus_sha256": self.identity_corpus_sha256,
                "bm25": {"k1": 1.2, "b": 0.75, "idf": "log1p-lucene"},
            },
            "vector_identity": vector_identity,
            "search_config": {"candidate_k": self.candidate_k, "rrf_c": self.rrf_c},
            "policy": {"name": "rule", "policy_version": self.policy_version,
                       "requires_statistics": False},
            "serving": {"schema_version": 1, "profile": self.dataset, "host": "127.0.0.1",
                        "port": 0, "max_body_bytes": 65536, "request_timeout_s": 10.0,
                        "router_factory": "rule"},
        }

    def _validate(self, payload):
        if not isinstance(payload, dict):
            raise BadRequest("请求必须是 JSON 对象")
        unknown = set(payload) - ALLOWED_FIELDS
        if unknown:
            raise BadRequest(f"未知字段: {sorted(unknown)}")
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            raise BadRequest("query 必须是非空白字符串")
        if query.count('"') % 2:
            raise BadRequest("引号不匹配")
        mode = payload.get("mode", "lexical")
        if mode not in MODES:
            raise BadRequest("未知 mode")
        k = payload.get("k", 10)
        if type(k) is not int or not 1 <= k <= 200:
            raise BadRequest("k 必须是 1..200 的整数")
        offset = payload.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise BadRequest("offset 必须是非负整数")
        scorer = payload.get("scorer", "bm25")
        expander = payload.get("expander", "none")
        if mode != "lexical" and (scorer != "bm25" or expander != "none"):
            raise BadRequest("非 lexical 只允许 scorer=bm25、expander=none")
        if mode != "lexical" and offset + k > self.candidate_k:
            raise BadRequest("offset + k 超过 candidate_k")
        run_tag = payload.get("run_tag")
        if run_tag is not None and not isinstance(run_tag, str):
            raise BadRequest("run_tag 必须是字符串")
        return {"query": query, "mode": mode, "k": k, "offset": offset}

    def _coerce(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        query = payload.get("query")
        query = query if isinstance(query, str) else "fallback query"
        mode = payload.get("mode")
        mode = mode if mode in MODES else "lexical"
        k = payload.get("k")
        k = k if type(k) is int and 1 <= k <= 200 else 10
        offset = payload.get("offset")
        offset = offset if type(offset) is int and offset >= 0 else 0
        return {"query": query, "mode": mode, "k": k, "offset": offset}

    def search(self, payload):
        try:
            request = (self._coerce(payload) if self.accept_invalid_requests
                       else self._validate(payload))
        except BadRequest as error:
            return 400, {"error": str(error)}
        query, mode, k, offset = (request[key] for key in ("query", "mode", "k", "offset"))
        if not self.lexical_up and mode == "lexical":
            return 500, {"error": "lexical unavailable"}
        explicit = '"' in query
        oov = OOV_TOKEN in query.lower()
        lexical_hits = [] if (explicit or oov) else list(ALL_DOC_IDS)
        vector_ok = self.vector_configured and self.backend_up
        fallback = False
        if mode == "lexical":
            executed, used, reason, rows = "lexical", False, "forced_lexical", lexical_hits
        elif mode == "dense":
            if explicit:
                return 400, {"error": "dense 模式无法保证显式词法约束"}
            if not vector_ok:
                return self.unavailable_status, {"error": "vector unavailable",
                                                 "code": "vector_unavailable"}
            executed, used, reason, rows = "dense", True, "forced_dense", list(ALL_DOC_IDS)
        elif mode == "hybrid":
            if not vector_ok:
                return self.unavailable_status, {"error": "vector unavailable",
                                                 "code": "vector_unavailable"}
            executed, used, reason, rows = "hybrid", True, "forced_hybrid", lexical_hits
        elif explicit:
            executed, used, reason, rows = ("lexical", False, "explicit_constraints",
                                            lexical_hits)
        elif lexical_hits:
            executed, used, reason, rows = "lexical", False, "lexical_sufficient", lexical_hits
        elif vector_ok:
            executed, used, reason, rows = "hybrid", True, "no_lexical_hits", list(ALL_DOC_IDS)
        elif self.adaptive_falls_back:
            executed, used, reason, rows = "lexical", False, "vector_unavailable", []
            fallback = True
        else:
            executed, used, reason, rows = "hybrid", True, "no_lexical_hits", list(ALL_DOC_IDS)

        results = [
            {"doc_id": doc_id, "rank": offset + index + 1,
             "score": round(1.0 / (index + 1), 6), "title": "", "snippet": "",
             "highlights": []}
            for index, doc_id in enumerate(rows[offset:offset + k])
        ]
        return 200, {
            "api_version": "2.0",
            "took_ms": 1,
            "parsed": {"type": "terms", "terms": [query], "explicit_constraints": explicit},
            "expanded": [],
            "routing": {
                "requested_mode": mode,
                "executed_mode": executed,
                "reason": reason,
                "vector_used": used,
                "vector_attempted": used or fallback,
                "fallback": fallback,
                "score_kind": {"lexical": "bm25", "dense": "cosine", "hybrid": "rrf"}[executed],
                "candidate_k": self.candidate_k,
                "policy_version": self.policy_version,
            },
            "timings_ms": {"lexical": 0.1, "router": 0.1, "encode": 0.1, "vector": 0.1,
                           "fusion": 0.1},
            "results": results,
        }


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send_bytes(self, code, body, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send(self, code, payload):
            self._send_bytes(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                             "application/json; charset=utf-8")

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                self._send_bytes(200, b"<!doctype html><title>fake</title>", "text/html; charset=utf-8")
            elif path == "/health":
                self._send(200, {"status": "ok", "index": {
                    "dataset": state.dataset, "n_docs": state.n_docs,
                    "schema_version": 1, "analyzer_hash": state.analyzer_hash,
                }})
            elif path == "/index/info":
                self._send(200, {**state.manifest(), "retrieval": state.info()})
            elif state.serve_traversal and unquote(path) == "/static/../README.md":
                self._send_bytes(200, b"leaked", "text/plain; charset=utf-8")
            else:
                self._send(404, {"error": "unknown path"})

        def do_POST(self):
            if urlsplit(self.path).path != "/search":
                self._send(404, {"error": "unknown path"})
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send(400, {"error": "非法 JSON"})
                return
            status, body = state.search(payload)
            self._send(status, body)

    return Handler


class FakeService:
    def __init__(self, **overrides):
        self.state = FakeState(**overrides)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def probe_argv(endpoint, out, *, phase="healthy", lexical_only=False, extra=()):
    argv = ["--endpoint", endpoint, "--out", str(out), "--phase", phase,
            "--expect-dataset", DATASET, "--expect-n-docs", str(N_DOCS),
            "--expect-corpus-sha256", CORPUS_SHA256,
            "--expect-collection", COLLECTION, "--expect-hnsw-ef", str(HNSW_EF),
            "--query", QUERY, "--timeout", "5"]
    if lexical_only:
        argv.append("--lexical-only")
    argv.extend(extra)
    return argv


class OnlineAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "evidence.json"

    def service(self, **overrides):
        service = FakeService(**overrides)
        self.addCleanup(service.close)
        return service

    def run_probe(self, endpoint, *, phase="healthy", lexical_only=False, out=None):
        out = out or self.out
        # 探针把逐条 PASS/FAIL 打到 stdout；这里捕获，避免预期失败的用例污染测试输出。
        with contextlib.redirect_stdout(io.StringIO()) as summary:
            status = acceptance.main(probe_argv(endpoint, out, phase=phase,
                                                lexical_only=lexical_only))
        self.last_summary = summary.getvalue()
        self.assertIn("证据", self.last_summary)
        return status, json.loads(out.read_text(encoding="utf-8"))

    @staticmethod
    def failure_names(evidence):
        return {check["name"] for check in evidence["checks"] if check["status"] == "FAIL"}

    def test_healthy_vector_profile_passes_with_writeable_evidence(self):
        service = self.service()
        status, evidence = self.run_probe(service.endpoint)
        self.assertEqual(status, 0)
        self.assertEqual(evidence["status"], "PASS")
        self.assertEqual(evidence["phase"], "healthy")
        self.assertEqual(evidence["run_tag"], "online-acceptance")
        names = {check["name"] for check in evidence["checks"]}
        for name in ("health_is_lexical_liveness", "index_identity", "vector_identity",
                     "ui_root", "static_traversal_blocked", "mode_lexical", "mode_dense",
                     "mode_hybrid", "mode_adaptive", "pagination_lexical",
                     "oov_semantic_rescue", "explicit_constraints", "unknown_field",
                     "candidate_boundary", "syntax_error"):
            self.assertIn(name, names)
        self.assertEqual(evidence["failures"], [])
        self.assertTrue(all(check["status"] == "PASS" for check in evidence["checks"]))
        search_events = [event for event in evidence["events"] if event["path"] == "/search"]
        self.assertTrue(search_events)
        tagged = [event for event in search_events
                  if isinstance(event["request"], dict) and "run_tag" in event["request"]]
        # 每个探针检索请求都带 run_tag；错误矩阵故意构造的非法请求体除外。
        self.assertGreaterEqual(len(tagged), 20)
        self.assertTrue(all(event["request"]["run_tag"] == "online-acceptance"
                            for event in tagged))

    def test_identity_mismatch_fails_each_expected_field(self):
        cases = (
            ("index_identity", {"identity_dataset": "other-dataset"}),
            ("index_identity", {"identity_n_docs": N_DOCS - 1}),
            ("index_identity", {"identity_corpus_sha256": "0" * 64}),
            ("vector_identity", {"collection": "other_collection"}),
            ("vector_identity", {"hnsw_ef": 128}),
        )
        for check_name, overrides in cases:
            with self.subTest(check_name=check_name, overrides=overrides):
                out = Path(self.tmp.name) / f"{sorted(overrides)[0]}.json"
                service = self.service(**overrides)
                status, evidence = self.run_probe(service.endpoint, out=out)
                self.assertEqual(status, 1)
                self.assertEqual(evidence["status"], "FAIL")
                self.assertIn(check_name, self.failure_names(evidence))

    def test_down_phase_passes_when_backend_is_really_down(self):
        service = self.service(backend_up=False)
        status, evidence = self.run_probe(service.endpoint, phase="down")
        self.assertEqual(status, 0, evidence["failures"])
        names = {check["name"] for check in evidence["checks"]}
        self.assertIn("dense_unavailable_503", names)
        self.assertIn("hybrid_unavailable_503", names)
        self.assertIn("adaptive_explicit_fallback", names)
        # 启动身份回显不随后端存活变化：down 阶段仍必须核对 vector_identity。
        self.assertIn("vector_identity", names)

    def test_down_phase_fails_when_adaptive_hides_backend_failure(self):
        service = self.service(backend_up=False, adaptive_falls_back=False)
        status, evidence = self.run_probe(service.endpoint, phase="down")
        self.assertEqual(status, 1)
        self.assertIn("adaptive_explicit_fallback", self.failure_names(evidence))

    def test_down_phase_fails_when_lexical_degrades(self):
        service = self.service(backend_up=False, lexical_up=False)
        status, evidence = self.run_probe(service.endpoint, phase="down")
        self.assertEqual(status, 1)
        self.assertIn("lexical_serves_without_vector", self.failure_names(evidence))

    def test_down_phase_fails_on_wrong_503_contract(self):
        service = self.service(backend_up=False, unavailable_status=500)
        status, evidence = self.run_probe(service.endpoint, phase="down")
        self.assertEqual(status, 1)
        self.assertIn("dense_unavailable_503", self.failure_names(evidence))

    def test_recovered_phase_requires_vector_modes(self):
        service = self.service()
        status, evidence = self.run_probe(service.endpoint, phase="recovered")
        self.assertEqual(status, 0, evidence["failures"])
        names = {check["name"] for check in evidence["checks"]}
        self.assertIn("recovered_dense", names)
        self.assertIn("recovered_adaptive", names)

    def test_recovered_phase_fails_while_backend_still_down(self):
        service = self.service(backend_up=False)
        status, evidence = self.run_probe(service.endpoint, phase="recovered")
        self.assertEqual(status, 1)
        self.assertIn("recovered_dense", self.failure_names(evidence))
        self.assertIn("recovered_adaptive", self.failure_names(evidence))

    def test_lexical_only_profile_expects_unavailable_vector_modes(self):
        service = self.service(vector_configured=False, backend_up=False)
        status, evidence = self.run_probe(service.endpoint, lexical_only=True)
        self.assertEqual(status, 0, evidence["failures"])
        names = {check["name"] for check in evidence["checks"]}
        self.assertNotIn("vector_identity", names)
        self.assertIn("dense_unavailable_503", names)

    def test_vector_profile_rejects_missing_vector_identity(self):
        service = self.service(vector_configured=False, backend_up=False)
        status, evidence = self.run_probe(service.endpoint)
        self.assertEqual(status, 1)
        failures = self.failure_names(evidence)
        self.assertIn("index_identity", failures)
        self.assertIn("vector_identity", failures)

    def test_error_matrix_detects_missing_request_validation(self):
        service = self.service(accept_invalid_requests=True)
        status, evidence = self.run_probe(service.endpoint)
        self.assertEqual(status, 1)
        failures = self.failure_names(evidence)
        for name in ("unknown_field", "bad_k", "bool_k", "candidate_boundary"):
            self.assertIn(name, failures)

    def test_path_safety_check_detects_traversal_leak(self):
        service = self.service(serve_traversal=True)
        status, evidence = self.run_probe(service.endpoint)
        self.assertEqual(status, 1)
        self.assertIn("static_traversal_blocked", self.failure_names(evidence))

    def test_unreachable_endpoint_records_failure_evidence(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        endpoint = f"http://127.0.0.1:{port}"
        status, evidence = self.run_probe(endpoint)
        self.assertEqual(status, 1)
        self.assertEqual(evidence["status"], "FAIL")
        self.assertTrue(evidence["failures"])
        self.assertIn("health_is_lexical_liveness", evidence["failures"])


def _stdlib_imports(source):
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    return modules


class ProbeIsolationTest(unittest.TestCase):
    def test_probe_imports_only_stdlib(self):
        source = (ROOT / "scripts" / "online_acceptance.py").read_text(encoding="utf-8")
        modules = _stdlib_imports(source)
        self.assertTrue(modules, "探针必须显式 import 标准库模块")
        self.assertEqual(modules - set(sys.stdlib_module_names), set())
        for forbidden in ("online", "offline", "contracts", "adapters", "evaluation", "tests"):
            self.assertNotIn(forbidden, modules)

    def test_vector_profile_requires_identity_expectations(self):
        # 缺期望不能被默默跳过：向量 profile 必须显式给出 collection/指纹/beam。
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                acceptance.main(["--endpoint", "http://127.0.0.1:1", "--out", "/dev/null",
                                 "--expect-dataset", DATASET, "--expect-n-docs", str(N_DOCS),
                                 "--query", QUERY])
        self.assertEqual(raised.exception.code, 2)

    def test_lexical_only_rejects_recovered_phase(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                acceptance.main(["--endpoint", "http://127.0.0.1:1", "--out", "/dev/null",
                                 "--lexical-only", "--phase", "recovered", "--expect-dataset",
                                 DATASET, "--expect-n-docs", str(N_DOCS), "--query", QUERY])
        self.assertEqual(raised.exception.code, 2)

    def test_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts" / "online_acceptance.py"), "--help"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--endpoint", result.stdout)
        self.assertIn("--phase", result.stdout)


class ExampleServingConfigTest(unittest.TestCase):
    """示例配置只证明冻结的 profile 形状，不代表已验证的真实部署。"""

    TOP_LEVEL = {"schema_version", "dataset", "index", "vector", "embedding", "retrieval",
                 "serve", "policy"}

    def test_examples_match_frozen_profile(self):
        cases = (
            ("news20k", "rfc4-clean-news-20k"),
            ("duretrieval-official", "dureader-official-dev-full-8096668"),
        )
        for name, dataset in cases:
            with self.subTest(name=name):
                path = ROOT / "deploy" / "config" / f"{name}.example.json"
                config = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(set(config), self.TOP_LEVEL)
                self.assertEqual(config["schema_version"], 1)
                self.assertEqual(config["dataset"], dataset)
                for value in (config["index"], config["vector"]["manifest"],
                              config["embedding"]["model_path"]):
                    self.assertIsInstance(value, str)
                    self.assertTrue(value)
                    self.assertFalse(Path(value).is_absolute(), value)
                    self.assertNotIn("$", value, "配置不允许环境变量插值")
                self.assertEqual(config["vector"]["url"], "http://127.0.0.1:6333")
                self.assertFalse(any(key in config["vector"]["url"]
                                     for key in ("@", "?", "#")))
                self.assertEqual(config["vector"]["hnsw_ef"], 256)
                self.assertEqual(config["vector"]["timeout_s"], 30.0)
                self.assertEqual(config["embedding"]["device"], "cpu")
                self.assertEqual(config["retrieval"], {"candidate_k": 200, "rrf_c": 60.0})
                self.assertEqual(config["serve"], {
                    "host": "127.0.0.1", "port": 8000, "max_body_bytes": 65536,
                    "request_timeout_s": 10.0,
                })
                self.assertEqual(config["policy"], {"factory": "rule"})


if __name__ == "__main__":
    unittest.main()
