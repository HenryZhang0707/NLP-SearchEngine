"""HTTP 服务（在线组）。只用标准库，零依赖。

    python -m online.server --index fixtures/tiny_index --port 8000

    curl -s localhost:8000/health
    curl -s localhost:8000/index/info
    curl -s -X POST localhost:8000/search \
        -H 'Content-Type: application/json' \
        -d '{"query": "心肌梗死 症状", "k": 5}'
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from contracts.api import ApiError, SearchRequest
from contracts.retrieval import RetrievalConfig, RetrievalUnavailable
from offline.disk_index import DiskIndex

from .engine import SearchEngine
from .expanders import ExpanderNotReady
from .parser import QuerySyntaxError


def make_handler(engine: SearchEngine):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # 安静一点
            pass

        def do_GET(self):
            reader = engine.reader
            if self.path == "/health":
                m = reader.manifest()
                self._send(200, {"status": "ok", "index": {
                    "dataset": m["dataset"],
                    "n_docs": m["n_docs"],
                    "schema_version": m["schema_version"],
                    "analyzer_hash": m["analyzer"]["config_hash"],
                }})
            elif self.path == "/index/info":
                self._send(200, {**reader.manifest(), "retrieval": engine.info()})
            else:
                self._send(404, {"error": "unknown path"})

        def do_POST(self):
            if self.path != "/search":
                self._send(404, {"error": "unknown path"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                req = SearchRequest.from_dict(payload)
            except (json.JSONDecodeError, ApiError, ValueError) as e:
                self._send(400, {"error": str(e)})
                return
            try:
                resp = engine.search(req)
            except (QuerySyntaxError, ApiError) as e:
                self._send(400, {"error": str(e)})
                return
            except ExpanderNotReady as e:
                self._send(501, {"error": str(e)})
                return
            except RetrievalUnavailable as e:
                self._send(503, {"error": str(e), "code": "vector_unavailable"})
                return
            self._send(200, resp.to_dict())

    return Handler


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="搜索引擎 HTTP 服务")
    ap.add_argument("--index", required=True)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--candidate-k", type=int, default=200)
    ap.add_argument("--rrf-c", type=float, default=60.0)
    args = ap.parse_args()

    # 真实向量适配器尚未接入 CLI；词法服务可独立运行，强制语义模式返回 503。
    engine = SearchEngine(DiskIndex(args.index), config=RetrievalConfig(
        candidate_k=args.candidate_k, rrf_c=args.rrf_c))
    print(f"索引已加载（{engine.reader.n_docs} 篇），监听 :{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(engine)).serve_forever()
