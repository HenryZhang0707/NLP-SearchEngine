import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from evaluation import audit_crawled_dataset, score_news_review


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.encode("utf-8") if isinstance(value, str) else json.dumps(value).encode("utf-8")
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


class NewsReviewScoringTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        dev = [f"dev-{i}" for i in range(50)]
        test = ["first", "no-positive"]
        self.grades = {(qid, "answer"): 2 for qid in dev}
        self.grades.update({("first", "answer"): 2, ("no-positive", "zero"): 0})
        selection_sha = put(self.root / "news-query-design-20261102/selected-200.manifest.json",
                            {"splits": {"dev": dev, "test": test}, "selected_queries_sha256": "queries"})
        pool = self.root / "news-formal-http-pool-rm3-resume-20261103/pool"
        provenance_sha = put(pool / "pool-provenance.jsonl", "".join(
            json.dumps({"query_id": qid, "doc_id": doc}) + "\n" for qid, doc in self.grades))
        pool_sha = put(pool / "manifest.json",
                       {"files": {"pool-provenance.jsonl": provenance_sha}, "pool_pairs": len(self.grades)})
        old_sha = put(self.root / "news-formal-pooled-qrels-20261104/manifest.json",
                      {"pool_manifest_sha256": pool_sha, "selected_queries_sha256": "queries"})
        self.runs = self.root / "news-formal-quality-test-20261104/formal-test"
        arms = {}
        for arm in audit_crawled_dataset.NEWS_ARMS:
            first = ["answer", "unjudged"] if arm == "adaptive" else ["unjudged", "answer"]
            rankings = dict(zip(test, (first, ["zero", "unjudged"]), strict=True))
            run = "".join(f"{qid} Q0 {doc} {rank} 1 {arm}\n" for qid, top in rankings.items()
                          for rank, doc in enumerate(top + [f"filler-{i}" for i in range(98)], 1))
            arms[arm] = {"sha256": put(self.runs / f"{arm}.trec", run)}
        run_state_sha = put(self.runs / "run-state.json", {
            "status": "COMPLETE", "split": "test", "n_queries": 2, "metrics_cutoff": 10,
            "retrieval_and_map_cutoff": 100, "pool_manifest_sha256": pool_sha,
            "selected_queries_sha256": "queries", "qrels_manifest_sha256": old_sha, "arms": arms,
        })
        for module, values in ((score_news_review, dict(
                SELECTED_MANIFEST_SHA256=selection_sha, QRELS_MANIFEST_SHA256=old_sha,
                RUN_STATE_SHA256=run_state_sha, REVIEW_PAIRS=len(self.grades),
                REVIEW_OVERLAP=2, TEST_PAIRS=2, TEST_QUERIES=2, BOOTSTRAP_SAMPLES=100)),
                (audit_crawled_dataset, {"RUN_STATE_SHA256": run_state_sha})):
            constants = patch.multiple(module, **values)
            constants.start()
            self.addCleanup(constants.stop)
        self.qrels = self.root / "review.qrels"
        self.intake = self.root / "intake.json"
        self.output = self.root / "scores.json"

    def bind_intake(self, *, adjudicate=True):
        digest = put(self.qrels, "".join(f"{qid}\t0\t{doc}\t{grade}\n"
                                        for (qid, doc), grade in self.grades.items()))
        return put(self.intake, {
            "schema_version": 1, "label_source": "reviewer_submissions",
            "plan": {"sha256": score_news_review.REVIEW_PLAN_SHA256},
            "packet": {"sha256": score_news_review.REVIEW_PACKET_SHA256},
            "private_map": {"sha256": score_news_review.REVIEW_MAP_SHA256},
            "pair_count": len(self.grades), "overlap_pair_count": 2,
            "disputed_pair_count": 1,
            "adjudication": {"pair_count": 1, "sha256": "a" * 64} if adjudicate else None,
            "reviewers": {name: {"pair_count": count, "sha256": "b" * 64}
                          for name, count in score_news_review.REVIEW_ASSIGNMENTS.items()},
            "qrels": {"sha256": digest},
        })

    def run_cli(self, digest):
        score_news_review.main([str(self.root), "--qrels", str(self.qrels), "--intake", str(self.intake),
                                "--intake-sha256", digest, "--out", str(self.output)])

    def test_scores_only_test_queries_and_counts_zero_grade_as_judged(self):
        self.run_cli(self.bind_intake())
        result = json.loads(self.output.read_bytes())
        self.assertEqual(result["test_queries"], 2)
        self.assertEqual(result["test_queries_without_positive"], 1)
        self.assertEqual(result["test_grade_counts"], {"0": 1, "2": 1})
        self.assertEqual(result["metrics"]["adaptive"]["ndcg_at_10"], .5)
        self.assertEqual(result["metrics"]["hybrid"]["mrr_at_10"], .25)
        self.assertEqual(result["judged_coverage"]["adaptive"]["10"], {"judged": 2, "slots": 20})
        self.assertEqual(result["adaptive_minus_hybrid"]["mrr_at_10"]["observed_difference"], .25)
        self.assertEqual(result["adaptive_minus_hybrid"]["recall_at_10"]["n_queries"], 2)

    def test_rejects_rebound_wrong_pool_and_tampered_rankings(self):
        self.grades["first", "wrong"] = self.grades.pop(("first", "answer"))
        with self.assertRaisesRegex(ValueError, "exact frozen news pool"):
            self.run_cli(self.bind_intake())
        self.assertFalse(self.output.exists())
        with (self.runs / "adaptive.trec").open("ab") as output:
            output.write(b"tampered\n")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch for adaptive"):
            self.run_cli(self.bind_intake())
        self.assertFalse(self.output.exists())

    def test_rejects_unadjudicated_receipt_even_with_matching_digest(self):
        with self.assertRaisesRegex(ValueError, "adjudication does not cover"):
            self.run_cli(self.bind_intake(adjudicate=False))
        self.assertFalse(self.output.exists())
