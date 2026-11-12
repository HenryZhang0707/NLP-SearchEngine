"""The last HTTP pair seals a complete capture before offline scoring."""

from pathlib import Path
import sqlite3
from contextlib import closing
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from evaluation import ce_http


class FixedCEResumeTests(unittest.TestCase):
    def test_final_identity_check_precedes_last_commit_and_offline_replay(self):
        identity = {"common": {"retrieval": {"fixture": "frozen"}}}

        def capture(client, arms, record, index, order, retrieval):
            return {
                name: {"ranking": [("doc", 0.5)], "ce_pairs": int(depth > 0),
                       "timings_ms": {}, "http_ms": 1.0, "explicit_constraints": False}
                for name, (depth, _) in arms.items()
            }

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "queries.tsv").write_text("q1\tone\nq2\ttwo\n", encoding="utf-8")
            (root / "warmup.tsv").write_text("w1\twarmup\n", encoding="utf-8")
            (root / "qrels.tsv").write_text("q1 0 doc 1\nq2 0 doc 1\n", encoding="utf-8")
            out = root / "output"
            args = [
                "--baseline", "http://127.0.0.1:18000",
                "--rerank", "28", "http://127.0.0.1:18028",
                "--queries", str(root / "queries.tsv"),
                "--warmup-queries", str(root / "warmup.tsv"),
                "--qrels", str(root / "qrels.tsv"), "--out", str(out),
            ]
            with (patch.object(ce_http, "check_services", side_effect=[identity, OSError("stopped")]),
                  patch.object(ce_http, "capture_query", side_effect=capture)):
                with self.assertRaisesRegex(OSError, "stopped"):
                    ce_http.main(args)
            self.assertFalse((out / "summary.json").exists())
            with closing(sqlite3.connect(out / "capture.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT qid FROM queries").fetchall(), [("q1",)])
            with (patch.object(ce_http, "check_services", return_value=identity),
                  patch.object(ce_http, "capture_query", side_effect=capture) as requests):
                ce_http.main(args)
            self.assertEqual([call.args[2].qid for call in requests.call_args_list], ["w1", "q2"])
            expected = (out / "summary.json").read_bytes()
            with (patch.object(ce_http, "check_services", side_effect=AssertionError("network used")),
                  patch.object(ce_http, "capture_query", side_effect=AssertionError("recaptured"))):
                ce_http.main(args)
            self.assertEqual((out / "summary.json").read_bytes(), expected)
