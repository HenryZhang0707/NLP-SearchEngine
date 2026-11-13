"""Transport failures stay incomplete and retain safe, discriminating causes."""
import errno
import json
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import patch
from urllib.error import URLError

from evaluation import evaluate


class TransportDiagnostics(unittest.TestCase):
    def failure(self, cause):
        marker = "fixture-private-url-and-password"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "requests.json"
            with patch.object(evaluate, "urlopen", side_effect=URLError(cause)):
                with self.assertRaises(evaluate.EvaluationError) as caught:
                    evaluate.evaluate_http("http://example.invalid/search", [evaluate.QueryRecord("q1", "alpha")],
                                           {"q1": {"d1": 1}}, request_output=output)
            raw = output.read_text(encoding="utf-8")
            self.assertNotIn(marker, raw)
            self.assertNotIn(marker, str(caught.exception))
            self.assertNotIn(marker, "".join(traceback.format_exception(caught.exception)))
            value = json.loads(raw)
            self.assertEqual(value["status"], "INCOMPLETE")
            self.assertEqual(value["http_200"], 0)
            self.assertEqual(value["unexpected_errors"], 1)
            event = value["last_outcome"]
            self.assertIsNone(event["http_status"])
            self.assertEqual(event["error_type"], "EvaluationError")
            self.assertGreaterEqual(event["latency_ms"], 0)
            return event["transport_error"]

    def test_socket_timeout_is_explicit_without_exception_text(self):
        detail = self.failure(TimeoutError("fixture-private-url-and-password"))
        self.assertEqual(detail["kind"], "timeout")
        self.assertEqual(detail["cause_type"], "TimeoutError")

    def test_connection_errno_is_preserved_without_exception_text(self):
        cause = OSError(errno.ECONNRESET, "fixture-private-url-and-password")
        detail = self.failure(cause)
        self.assertEqual(detail["kind"], "transport")
        self.assertEqual(detail["cause_type"], type(cause).__name__)
        self.assertEqual(detail["errno"], errno.ECONNRESET)


if __name__ == "__main__":
    unittest.main()
