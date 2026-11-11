import hashlib
from importlib.util import find_spec
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from contracts.api import SearchRequest
from evaluation.bm25s_reference import generate_run
from evaluation.evaluate import load_run
from offline.build import build
from offline.disk_index import DiskIndex
from online.engine import SearchEngine


@unittest.skipUnless(find_spec("bm25s"), "optional BM25S reference")
class BM25SReferenceTests(unittest.TestCase):
    def test_scores_and_order_match_with_zero_tokens_ties_and_constraints(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus.jsonl"
            texts = ("...", "a a a", "a b", "b c", "a a a")
            rows = [dict(doc_id=str(i), title="", text=text) for i, text in enumerate(texts)]
            data = "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")
            corpus.write_bytes(data)
            queries = root / "queries.tsv"
            cases = ["a a", "NOT a", "a AND b", '"a b"', "qqq"]
            queries.write_text("".join(f"q{i}\t{query}\n" for i, query in enumerate(cases)),
                               encoding="utf-8")
            index = root / "index"
            build(corpus, index, "reference-regression")
            output = root / "reference"
            options = {"corpus_sha256": hashlib.sha256(data).hexdigest(), "n_docs": len(rows),
                       "depth": len(rows)}
            with self.assertRaisesRegex(ValueError, "constraint-index"):
                generate_run(corpus, queries, output, **options)
            self.assertFalse(output.exists())

            report = generate_run(corpus, queries, output, constraint_index=index, **options)
            run_file = output / "run.trec"
            self.assertEqual(report["run_sha256"], hashlib.sha256(run_file.read_bytes()).hexdigest())
            self.assertNotIn(b"\r", run_file.read_bytes())
            run = load_run(run_file)
            engine = SearchEngine(DiskIndex(index))
            for i, query in enumerate(cases):
                with self.subTest(query=query):
                    hits = engine.search(SearchRequest(query=query, mode="lexical", k=len(rows))).results
                    reference = run.get(f"q{i}", [])
                    self.assertEqual([doc_id for doc_id, _ in reference], [hit.doc_id for hit in hits])
                    for (_, score), hit in zip(reference, hits):
                        self.assertTrue(math.isclose(score, hit.score, abs_tol=1e-12, rel_tol=1e-12))

            other = root / "other.jsonl"
            other.write_bytes(data.replace(b"a b", b"x y"))
            wrong_index = root / "wrong-index"
            build(other, wrong_index, "reference-regression")
            with self.assertRaisesRegex(ValueError, "same ordered corpus"):
                generate_run(corpus, queries, root / "bad", constraint_index=wrong_index, **options)
            self.assertFalse((root / "bad").exists())
