"""Legacy crawler network-boundary regressions; only loopback HTTP is used."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from evaluation.crawl_web import CrawlConfig, CrawlError, _read_url, _robots_checker, crawl


@contextmanager
def site(responder):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("User-Agent")))
            status, headers, body = responder(self.path)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass  # bounded clients may close before an oversized body ends

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def robots_response(_path):
    return 200, {"Content-Type": "text/plain"}, b"User-agent: *\nAllow: /\n"


class CrawlWebSecurityTests(unittest.TestCase):
    def test_invalid_urls_are_rejected_before_open(self):
        for url in ("file:///tmp/not-crawlable", "ftp://127.0.0.1/", "http://user:pass@127.0.0.1/"):
            with self.subTest(url=url), patch("evaluation.crawl_web._open_url") as opened:
                with self.assertRaises(CrawlError):
                    _read_url(url, CrawlConfig(delay_seconds=0))
                opened.assert_not_called()

    def test_page_redirects_never_request_same_or_cross_origin_targets(self):
        with site(lambda _path: (200, {"Content-Type": "text/html"}, b"target")) as (sink, sink_requests):
            for status in (301, 302, 303, 307, 308):
                for location in ("/target", sink + "/target"):
                    with self.subTest(status=status, location=location):
                        def respond(path):
                            if path == "/start":
                                return status, {"Location": location}, b""
                            return 200, {"Content-Type": "text/html"}, b"target"
                        with site(respond) as (origin, requests):
                            with self.assertRaises(CrawlError):
                                _read_url(origin + "/start", CrawlConfig(delay_seconds=0))
                            self.assertEqual([p for p, _ in requests], ["/start"])
            self.assertEqual(sink_requests, [])

    def test_robots_redirects_fail_closed_without_following_and_cache_failures(self):
        with site(robots_response) as (sink, sink_requests):
            for location in ("/target", sink + "/target"):
                for allow_failure in (False, True):
                    with self.subTest(location=location, allow_failure=allow_failure):
                        def respond(path):
                            if path == "/robots.txt":
                                return 302, {"Location": location}, b""
                            return robots_response(path)
                        with site(respond) as (origin, requests):
                            allowed = _robots_checker(CrawlConfig(delay_seconds=0, allow_robots_failure=allow_failure))
                            self.assertEqual(allowed(origin + "/page"), allow_failure)
                            self.assertEqual(allowed(origin + "/other"), allow_failure)
                            self.assertEqual([p for p, _ in requests], ["/robots.txt"])
            self.assertEqual(sink_requests, [])

    def test_robots_http_policy_and_configured_user_agent(self):
        for status, expected in ((401, False), (403, False), (404, True)):
            with self.subTest(status=status), site(lambda _path: (status, {}, b"")) as (origin, requests):
                config = CrawlConfig(delay_seconds=0, user_agent="bounded-test-bot", allow_robots_failure=True)
                self.assertEqual(_robots_checker(config)(origin + "/page"), expected)
                self.assertEqual(requests, [("/robots.txt", "bounded-test-bot")])

    def test_oversized_robots_are_denied_by_default(self):
        body = b"User-agent: *\nAllow: /\n#" + b"x" * 200
        with site(lambda _path: (200, {"Content-Type": "text/plain"}, body)) as (origin, requests):
            self.assertFalse(_robots_checker(CrawlConfig(max_bytes=64, delay_seconds=0))(origin + "/page"))
            self.assertEqual(len(requests), 1)

    def test_robots_timeout_is_bounded_and_failure_policy_is_explicit(self):
        with site(robots_response) as (origin, _requests):
            for allow_failure in (False, True):
                with self.subTest(allow_failure=allow_failure), patch(
                    "evaluation.crawl_web._open_url", create=True, side_effect=TimeoutError("bounded")
                ) as opened:
                    config = CrawlConfig(timeout=0.25, delay_seconds=0, allow_robots_failure=allow_failure)
                    allowed = _robots_checker(config)
                    self.assertEqual(allowed(origin + "/page"), allow_failure)
                    self.assertEqual(allowed(origin + "/other"), allow_failure)
                    self.assertEqual(opened.call_count, 1)
                    self.assertEqual(opened.call_args.kwargs["timeout"], config.timeout)
                    self.assertEqual(opened.call_args.args[0].get_header("User-agent"), config.user_agent)

    def test_pacing_covers_robots_failed_non_html_and_leaf_requests(self):
        calls = []
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        def fetch(url, config):
            path = urlsplit(url).path
            calls.append((path, clock[0]))
            if path == "/robots.txt":
                return "text/plain", "User-agent: *\nAllow: /\n"
            if path == "/fail":
                raise CrawlError("unavailable")
            if path == "/asset":
                return "image/png", "not HTML"
            if path == "/page":
                return "text/html", '<a href="/leaf">page</a>'
            return "text/html", "leaf"

        with site(robots_response) as (origin, _requests), patch(
            "evaluation.crawl_web._read_url", side_effect=fetch
        ), patch("evaluation.crawl_web.time.monotonic", side_effect=lambda: clock[0]), patch(
            "evaluation.crawl_web.time.sleep", side_effect=sleep
        ):
            pages = crawl([origin + path for path in ("/fail", "/asset", "/page")],
                          CrawlConfig(max_documents=2, max_depth=1, delay_seconds=2))
        self.assertEqual([p.url for p in pages], [origin + "/page", origin + "/leaf"])
        self.assertEqual(calls, [("/robots.txt", 0), ("/fail", 2), ("/asset", 4), ("/page", 6), ("/leaf", 8)])

    def test_nonfinite_network_budgets_are_rejected(self):
        for field in ("timeout", "delay_seconds"):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, value=value), self.assertRaises(CrawlError):
                    CrawlConfig(**{field: value})


if __name__ == "__main__":
    unittest.main()
