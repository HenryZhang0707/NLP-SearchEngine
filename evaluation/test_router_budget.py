"""Regression tests for the offline exact-vector-budget routing control."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation.evaluate import EvaluationError, QueryRouting, evaluate_http, load_qrels, load_queries
from evaluation.router_budget import compare_router_budget


class RouterBudgetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.files = {name: root / filename for name, filename in {
            "queries": "queries.tsv", "qrels": "qrels.tsv", "lexical_run": "lexical.trec",
            "hybrid_run": "hybrid.trec", "adaptive_run": "adaptive.trec",
            "adaptive_receipt": "adaptive.trec.requests.json",
        }.items()}
        self.metrics_path = root / "metrics.json"
        self.constraints = root / "lexical_only.json"
        self.constraints.write_text('["q2"]\n', encoding="utf-8")
        self.files["queries"].write_text("q1\tfirst\nq2\tsecond\n", encoding="utf-8")
        self.files["qrels"].write_text("q1 0 hit 1\nq2 0 hit 1\n", encoding="utf-8")
        self.branches = {"lexical": [["miss", "extra"], ["hit", "extra"]],
                         "hybrid": [["hit", "extra"], ["miss", "extra"]]}
        for mode, rankings in self.branches.items():
            self.write_run(mode, rankings)

    def write_run(self, mode, rankings):
        self.files[f"{mode}_run"].write_text("".join(
            f"q{qid} Q0 {docid} {rank} 1 fixture\n"
            for qid, ranking in enumerate(rankings, 1) for rank, docid in enumerate(ranking, 1)
        ), encoding="utf-8")

    def evaluate_adaptive(self, reasons=("small_relative_gap", "lexical_sufficient")):
        transport = []
        for index, reason in enumerate(reasons):
            vector = reason in ("small_relative_gap", "no_lexical_hits")
            fallback = reason == "vector_unavailable"
            mode = "hybrid" if vector else "lexical"
            route = QueryRouting("adaptive", mode, reason, vector or fallback, vector, fallback)
            transport.append(([(docid, 1.0) for docid in self.branches[mode][index]], 2.0, route))
        with patch("evaluation.evaluate._request_search", side_effect=transport):
            result = evaluate_http("http://unused", load_queries(self.files["queries"]),
                                   load_qrels(self.files["qrels"]), mode="adaptive",
                                   run_output=self.files["adaptive_run"])
        self.metrics_path.write_text(json.dumps(result.to_dict(include_per_query=True)), encoding="utf-8")

    def run_budget(self, **overrides):
        return compare_router_budget(**(self.files | overrides), k=1, map_k=1, samples=100, seed=917)

    def test_matched_budget_has_analytical_expected_quality_and_paired_interval(self):
        self.evaluate_adaptive()
        result = self.run_budget()
        self.assertEqual(result, self.run_budget())
        self.assertEqual(result["vector_budget"]["eligible_queries"], 2)
        self.assertEqual(result["vector_budget"]["uniform_fixed_budget_calls"], 1)
        self.assertEqual(result["vector_budget"]["observed_adaptive_calls"], 1)
        self.assertEqual(result["vector_budget"]["eligible_inclusion_probability"], 0.5)
        for metric in result["metrics"].values():
            self.assertEqual(metric["forced_lexical"], 0.5)
            self.assertEqual(metric["constraint_aware_always_hybrid"], 0.5)
            self.assertEqual(metric["observed_adaptive"], 1.0)
            self.assertEqual(metric["expected_uniform_fixed_budget"], 0.5)
            self.assertEqual(metric["adaptive_minus_expected_random"], 0.5)
            self.assertEqual(metric["paired_query_bootstrap_ci"], [0.5, 0.5])
        from_metrics = self.run_budget(adaptive_receipt=self.metrics_path)
        self.assertEqual(result["metrics"], from_metrics["metrics"])
        self.assertEqual(result["coverage"], from_metrics["coverage"])

    def test_zero_budget(self):
        self.evaluate_adaptive(("lexical_sufficient", "lexical_sufficient"))
        report = self.run_budget()
        self.assertEqual(report["vector_budget"]["uniform_fixed_budget_calls"], 0)
        for metric in report["metrics"].values():
            self.assertEqual(metric["expected_uniform_fixed_budget"], metric["forced_lexical"])
            self.assertEqual(metric["paired_query_bootstrap_ci"], [0.0, 0.0])

    def test_full_budget(self):
        self.evaluate_adaptive(("small_relative_gap", "small_relative_gap"))
        report = self.run_budget()
        self.assertEqual(report["vector_budget"]["uniform_fixed_budget_calls"], 2)
        for metric in report["metrics"].values():
            self.assertEqual(metric["expected_uniform_fixed_budget"], metric["constraint_aware_always_hybrid"])
            self.assertEqual(metric["paired_query_bootstrap_ci"], [0.0, 0.0])

    def test_lexical_only_constraints(self):
        self.evaluate_adaptive(("small_relative_gap", "explicit_constraints"))
        with self.assertRaisesRegex(EvaluationError, "explicit_constraints"):
            self.run_budget()
        result = self.run_budget(lexical_only=self.constraints)
        self.assertEqual(result["coverage"]["lexical_only_queries"], 1)
        self.assertEqual(result["vector_budget"]["eligible_queries"], 1)
        self.assertEqual(result["vector_budget"]["uniform_fixed_budget_calls"], 1)
        self.assertEqual(result["metrics"]["ndcg_at_1"]["constraint_aware_always_hybrid"], 1.0)

    def test_all_queries_lexical_only(self):
        self.constraints.write_text('["q1", "q2"]\n', encoding="utf-8")
        self.evaluate_adaptive(("explicit_constraints", "explicit_constraints"))
        result = self.run_budget(lexical_only=self.constraints)
        self.assertEqual(result["vector_budget"]["eligible_queries"], 0)
        self.assertEqual(result["vector_budget"]["uniform_fixed_budget_calls"], 0)
        self.assertEqual(result["metrics"]["ndcg_at_1"]["adaptive_minus_expected_random"], 0.0)

    def test_fallback_and_unknown_reasons_rejected(self):
        self.evaluate_adaptive(("vector_unavailable", "unexpected"))
        with self.assertRaisesRegex(EvaluationError, "fallback"):
            self.run_budget()
        for name in ("adaptive_run", "adaptive_receipt"):
            self.files[name].unlink()
        self.files["adaptive_receipt"].with_suffix(".json.events.jsonl").unlink()
        self.evaluate_adaptive(("lexical_sufficient", "unexpected"))
        with self.assertRaisesRegex(EvaluationError, "Unknown or inconsistent"):
            self.run_budget()

    def test_rank_mismatch_and_noncovering_run_rejected(self):
        self.evaluate_adaptive()
        self.write_run("adaptive", [["extra", "hit"], ["hit", "extra"]])
        with self.assertRaisesRegex(EvaluationError, "selected branch"):
            self.run_budget()
        self.write_run("adaptive", [["hit", "extra"], ["hit", "extra"]])
        self.write_run("hybrid", [["hit", "extra"], []])
        with self.assertRaisesRegex(EvaluationError, "Uncorroborated empty hybrid"):
            self.run_budget()

    def test_trec_empty_is_accepted_when_branches_and_receipt_agree(self):
        self.branches["lexical"][0] = []
        self.write_run("lexical", self.branches["lexical"])
        self.evaluate_adaptive(("explicit_constraints", "lexical_sufficient"))
        self.constraints.write_text('["q1"]\n', encoding="utf-8")
        result = self.run_budget(adaptive_receipt=self.metrics_path, lexical_only=self.constraints)
        self.assertEqual(result["coverage"]["empty_rankings"], {"lexical": 1, "adaptive": 1, "hybrid": 0})

    def test_receipt_tamper_and_foreign_judgment_rejected(self):
        self.evaluate_adaptive()
        path = self.files["adaptive_receipt"]
        raw = path.read_bytes()
        state = json.loads(raw)
        state["attempted_requests"] += 1
        path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(EvaluationError, "counters"):
            self.run_budget()
        path.write_bytes(raw)
        journal = path.with_name(state["journal"]["file"])
        journal.write_bytes(journal.read_bytes() + b"{}\n")
        with self.assertRaisesRegex(EvaluationError, "journal SHA256"):
            self.run_budget()
        self.files["qrels"].write_text("q1 0 hit 1\nforeign 0 hit 1\n", encoding="utf-8")
        with self.assertRaisesRegex(EvaluationError, "exact query universe"):
            self.run_budget()


if __name__ == "__main__":
    unittest.main()
