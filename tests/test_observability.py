"""Observability and HTTP request-hardening tests with no real models/services."""

from http.server import ThreadingHTTPServer
import io
import json
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from online.engine import SearchEngine
from online.memory_index import MemoryIndex
from online.observability import RUN_TAG_LIMIT, SearchLogger
from online.server import make_handler


CORPUS = [
    {"doc_id": "d0", "title": "", "text": "phone water"},
    {"doc_id": "d1", "title": "", "text": "blue apple"},
]
TIMING_STAGES = ("lexical", "router", "encode", "vector", "fusion")


class SearchLoggerTests(unittest.TestCase):
    def test_record_is_bounded_escaped_jsonl_without_raw_payloads(self):
        sink = io.StringIO()
        logger = SearchLogger(sink)
        logger.search(
            status=200, run_tag='line\n"' + "x" * 300, requested_mode="adaptive",
            executed_mode="hybrid", reason="low_vocab_coverage",
            vector_attempted=True, vector_used=True, fallback=False,
            policy_version="rules-v1", dataset="memory", corpus_sha256="a" * 64,
            took_ms=5, timings_ms={name: float(i + 1) for i, name in
                                   enumerate(TIMING_STAGES)},
            serialization_ms=0.25, http_elapsed_ms=2.5,
        )
        lines = sink.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["event"], "search")
        self.assertEqual(record["status"], 200)
        self.assertLessEqual(len(record["run_tag"]), RUN_TAG_LIMIT)
        self.assertEqual(record["requested_mode"], "adaptive")
        self.assertEqual(record["executed_mode"], "hybrid")
        self.assertEqual(record["reason"], "low_vocab_coverage")
        self.assertEqual(record["vector_attempted"], True)
        self.assertEqual(record["vector_used"], True)
        self.assertEqual(record["fallback"], False)
        self.assertEqual(record["policy_version"], "rules-v1")
        self.assertEqual(record["dataset"], "memory")
        self.assertEqual(record["corpus_identity"], "a" * 12)
        self.assertEqual(record["took_ms"], 5)
        self.assertEqual(set(record["timings_ms"]), set(TIMING_STAGES))
        self.assertEqual(record["serialization_ms"], 0.25)
        self.assertEqual(record["http_elapsed_ms"], 2.5)
        for forbidden in ("query", "headers", "body", "exception", "Authorization"):
            self.assertNotIn(forbidden, record)

    def test_default_sink_is_stderr(self):
        sink = io.StringIO()
        with patch("sys.stderr", sink):
            logger = SearchLogger()
            logger.search(status=400, reason="invalid_request")
        record = json.loads(sink.getvalue())
        self.assertEqual(record["status"], 400)
        self.assertEqual(record["run_tag"], "")

    def test_writes_are_serialized_across_threads(self):
        sink = io.StringIO()
        logger = SearchLogger(sink)

        def emit(index):
            for offset in range(5):
                logger.search(status=200, run_tag=f"{index}-{offset}",
                              requested_mode="lexical",
                              executed_mode="lexical")

        threads = [threading.Thread(target=emit, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        lines = sink.getvalue().splitlines()
        self.assertEqual(len(lines), 100)
        records = [json.loads(line) for line in lines]
        self.assertEqual({record["event"] for record in records}, {"search"})
        self.assertEqual(len({record["run_tag"] for record in records}), 100)

    def test_invalid_timing_values_are_not_emitted_as_nan(self):
        sink = io.StringIO()
        logger = SearchLogger(sink)
        logger.search(status=200, timings_ms={"lexical": float("nan"), "router": "x"},
                      serialization_ms=float("inf"), http_elapsed_ms=True)
        record = json.loads(sink.getvalue())
        self.assertEqual(record["timings_ms"], {name: None for name in TIMING_STAGES})
        self.assertIsNone(record["serialization_ms"])
        self.assertIsNone(record["http_elapsed_ms"])


class HttpServingTests(unittest.TestCase):
    def setUp(self):
        self.engine = SearchEngine(MemoryIndex.from_corpus(CORPUS))
        self.sink = io.StringIO()
        self.serving = {
            "schema_version": 1,
            "profile": "memory",
            "host": "127.0.0.1",
            "port": 0,
            "max_body_bytes": 65536,
            "request_timeout_s": 0.3,
            "router_factory": "rule",
        }
        handler = make_handler(
            self.engine, serving=self.serving, max_body_bytes=65536,
            request_timeout_s=0.3, search_log=SearchLogger(self.sink))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.serving["port"] = self.server.server_port
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get(self, path):
        with urlopen(self.base + path, timeout=5) as response:
            return response.status, json.load(response)

    def post(self, payload, headers=None):
        data = json.dumps(payload).encode("utf-8")
        request = Request(self.base + "/search", data,
                          {"Content-Type": "application/json", **(headers or {})})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def records(self):
        return [json.loads(line) for line in self.sink.getvalue().splitlines() if line]

    def wait_for_records(self, count, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            records = self.records()
            if len(records) >= count:
                return records
            time.sleep(0.01)
        return self.records()

    def raw_request(self, request: bytes, timeout=3.0):
        with socket.create_connection(("127.0.0.1", self.server.server_port),
                                      timeout=timeout) as sock:
            sock.sendall(request)
            sock.settimeout(timeout)
            data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        return int(data.split(b" ", 2)[1]), data

    def test_health_is_exact_and_info_carries_frozen_serving_block(self):
        status, health = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(set(health), {"status", "index"})
        self.assertEqual(set(health["index"]),
                         {"dataset", "n_docs", "schema_version", "analyzer_hash"})
        status, info = self.get("/index/info")
        self.assertEqual(status, 200)
        self.assertEqual(info["retrieval"]["serving"], self.serving)
        self.assertEqual(set(info["retrieval"]["serving"]), {
            "schema_version", "profile", "host", "port", "max_body_bytes",
            "request_timeout_s", "router_factory",
        })
        self.assertEqual(info["retrieval"]["serving"]["profile"], "memory")

    def test_success_log_has_five_stages_and_no_query_or_headers(self):
        status, body = self.post({"query": "SECRET_QUERY_TEXT", "run_tag": "run-1"})
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"api_version", "routing", "timings_ms", "took_ms",
                                     "parsed", "expanded", "results"})
        self.assertEqual(set(body["timings_ms"]), set(TIMING_STAGES))
        record = self.wait_for_records(1)[0]
        self.assertEqual(record["event"], "search")
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["run_tag"], "run-1")
        self.assertEqual(record["requested_mode"], "lexical")
        self.assertEqual(record["executed_mode"], "lexical")
        self.assertEqual(record["reason"], "forced_lexical")
        self.assertEqual(record["policy_version"], "rules-v1")
        self.assertEqual(record["dataset"], "memory")
        self.assertIsNone(record["corpus_identity"])
        self.assertEqual(set(record["timings_ms"]), set(TIMING_STAGES))
        self.assertIsInstance(record["serialization_ms"], (int, float))
        self.assertIsInstance(record["http_elapsed_ms"], (int, float))
        self.assertNotIn("SECRET_QUERY_TEXT", self.sink.getvalue())
        self.assertNotIn("Authorization", self.sink.getvalue())

    def test_400_501_503_and_plugin_500_are_logged_without_payload_or_exception(self):
        status, body = self.post({"query": "SECRET_QUERY_TEXT", "k": True})
        self.assertEqual(status, 400)
        status, body = self.post({"query": "SECRET_QUERY_TEXT", "mode": "dense"})
        self.assertEqual(status, 503)
        self.assertEqual(body["code"], "vector_unavailable")
        status, _ = self.post({"query": "SECRET_QUERY_TEXT", "expander": "slm"})
        self.assertEqual(status, 501)

        def explode(request):
            raise RuntimeError("SECRET_EXCEPTION /private/credentials")

        self.engine.search = explode
        status, body = self.post({"query": "SECRET_QUERY_TEXT"})
        self.assertEqual(status, 500)
        self.assertEqual(body, {"error": "internal server error"})
        # The thread/engine failure did not take down the server.
        self.assertEqual(self.get("/health")[0], 200)

        records = self.wait_for_records(4)
        by_status = {}
        for record in records:
            by_status.setdefault(record["status"], []).append(record)
        self.assertEqual(by_status[400][0]["reason"], "invalid_request")
        self.assertEqual(by_status[503][0]["reason"], "vector_unavailable")
        self.assertEqual(by_status[503][0]["requested_mode"], "dense")
        self.assertTrue(by_status[503][0]["vector_attempted"])
        self.assertEqual(by_status[501][0]["reason"], "expander_unavailable")
        self.assertEqual(by_status[500][0]["reason"], "internal_error")
        self.assertEqual(by_status[500][0]["policy_version"], "rules-v1")
        output = self.sink.getvalue()
        for secret in ("SECRET_QUERY_TEXT", "SECRET_EXCEPTION", "credentials",
                       "Traceback", "/private"):
            self.assertNotIn(secret, output)

    def test_bad_framing_oversize_and_timeout_are_rejected_before_search(self):
        cases = [
            (b"POST /search HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n", 400),
            (b"POST /search HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n", 400),
            (b"POST /search HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n"
             b"Content-Length: 2\r\n\r\n", 400),
            (b"POST /search HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n", 400),
            (b"POST /search HTTP/1.1\r\nHost: x\r\nContent-Length: 65537\r\n\r\n", 413),
        ]
        for request, expected in cases:
            with self.subTest(expected=expected, request=request):
                status, data = self.raw_request(request)
                self.assertEqual(status, expected)
                self.assertIn(b"Content-Length", data)
        records = self.wait_for_records(len(cases))
        self.assertEqual([record["status"] for record in records], [400, 400, 400, 400, 413])
        self.assertEqual(records[-1]["reason"], "payload_too_large")

    def test_invalid_utf8_body_is_sanitized_400_without_dropping_the_connection(self):
        calls = []
        real_search = self.engine.search
        self.engine.search = lambda request: calls.append(request) or real_search(request)
        status, data = self.raw_request(
            b"POST /search HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\n\r\n\xff")
        self.assertEqual(status, 400)
        self.assertIn(b"request body is not valid JSON", data)
        self.assertEqual(calls, [])
        record = self.wait_for_records(1)[-1]
        self.assertEqual(record["status"], 400)
        self.assertEqual(record["reason"], "invalid_request")
        # 同一个服务进程仍然可用，没有把异常升到连接/线程层。
        self.assertEqual(self.get("/health")[0], 200)
        self.assertEqual(self.post({"query": "water"})[0], 200)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("UnicodeDecodeError", self.sink.getvalue())

    def _assert_json_parser_failure_is_sanitized(self, body):
        self.assertLessEqual(len(body), self.serving["max_body_bytes"])
        request = (b"POST /search HTTP/1.1\r\nHost: x\r\nContent-Length: "
                   + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
        with patch.object(self.engine, "search", wraps=self.engine.search) as search, \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            status, data = self.raw_request(request)
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(data.split(b"\r\n\r\n", 1)[1]),
                             {"error": "request body is not valid JSON"})
            search.assert_not_called()
            record = self.wait_for_records(1)[0]
            self.assertEqual(record["status"], 400)
            self.assertEqual(record["reason"], "invalid_request")
            self.assertNotIn("SECRET_QUERY_TEXT", self.sink.getvalue())
            self.assertEqual(stderr.getvalue(), "")
        # A rejected payload must leave the same service ready for valid requests.
        self.assertEqual(self.get("/health")[0], 200)
        self.assertEqual(self.post({"query": "water"})[0], 200)

    @unittest.skipUnless(hasattr(sys, "set_int_max_str_digits"),
                         "Python does not enforce a JSON integer digit limit")
    def test_json_integer_digit_limit_is_sanitized_400_before_search(self):
        previous_limit = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(4300)
        self.addCleanup(sys.set_int_max_str_digits, previous_limit)
        body = b'{"query":"SECRET_QUERY_TEXT","k":' + b"9" * 5000 + b"}"
        self._assert_json_parser_failure_is_sanitized(body)

    def test_deeply_nested_json_is_sanitized_400_before_search(self):
        depth = sys.getrecursionlimit() + 100
        body = (b'{"query":"SECRET_QUERY_TEXT","k":'
                + b"[" * depth + b"0" + b"]" * depth + b"}")
        self._assert_json_parser_failure_is_sanitized(body)

    def test_content_length_digits_are_strict_ascii_and_bounded(self):
        calls = []
        real_search = self.engine.search
        self.engine.search = lambda request: calls.append(request) or real_search(request)
        cases = [
            # Latin-1 上标数字：str.isdigit() 为真但 int() 拒绝。
            (b"POST /search HTTP/1.1\r\nHost: x\r\nContent-Length: \xb2\r\n\r\n", 400),
            # 超过 int() 十进制转换上限（4300 位）：必须在解析前按超大体积拒绝。
            (b"POST /search HTTP/1.1\r\nHost: x\r\nContent-Length: "
             + b"9" * 5000 + b"\r\n\r\n", 413),
            # 前导零不应把有界转换变成 413：真实值 1，按 1 字节读取后判非法 JSON。
            (b"POST /search HTTP/1.1\r\nHost: x\r\nContent-Length: "
             + b"0" * 100 + b"1\r\n\r\n{", 400),
        ]
        for request, expected in cases:
            with self.subTest(expected=expected, length=request.split(b"\r\n")[2][:40]):
                status, _ = self.raw_request(request)
                self.assertEqual(status, expected)
        self.assertEqual(calls, [])
        records = self.wait_for_records(len(cases))
        self.assertEqual([record["status"] for record in records], [400, 413, 400])
        self.assertEqual(records[1]["reason"], "payload_too_large")
        self.assertEqual(self.get("/health")[0], 200)

    def test_socket_read_timeout_returns_408_and_closes(self):
        with socket.create_connection(("127.0.0.1", self.server.server_port),
                                      timeout=3) as sock:
            sock.sendall(b"POST /search HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\n{")
            sock.settimeout(3)
            data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        self.assertIn(b" 408 ", data.split(b"\r\n", 1)[0] + b" ")
        record = self.wait_for_records(1)[0]
        self.assertEqual(record["status"], 408)
        self.assertEqual(record["reason"], "request_timeout")

    def test_run_tag_is_bounded_and_escaped_and_bad_json_is_not_echoed(self):
        status, _ = self.post({"query": "SECRET_QUERY_TEXT",
                               "run_tag": "line\n" + "x" * 300})
        self.assertEqual(status, 200)
        record = self.wait_for_records(1)[0]
        self.assertLessEqual(len(record["run_tag"]), RUN_TAG_LIMIT)
        self.assertTrue(record["run_tag"].startswith("line\n"))

        status, _ = self.raw_request(
            b"POST /search HTTP/1.1\r\nHost: x\r\nContent-Length: 16\r\n\r\n"
            b'{"SECRET_BODY" }')
        self.assertEqual(status, 400)
        record = self.wait_for_records(2)[-1]
        self.assertEqual(record["status"], 400)
        self.assertNotIn("SECRET_BODY", self.sink.getvalue())


if __name__ == "__main__":
    unittest.main()
