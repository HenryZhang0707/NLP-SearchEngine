import hashlib
from pathlib import Path
import tempfile
import unittest

from evaluation.audit_crawled_dataset import exact_body_overlap, overlap


class CrawledDatasetAuditTest(unittest.TestCase):
    def test_query_overlap_distinguishes_normalized_exact_and_near(self):
        self.assertEqual(
            overlap(["ＡＢＣＤＥＦＧＨＩ！"], ["abcdefghi", "abcdefghij", "unrelated"]),
            {"compared_pairs": 3, "normalized_exact": 1, "nonexact_bigram_jaccard_ge_0_8": 1},
        )

    def test_exact_body_overlap_checks_full_count_and_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.jsonl"
            data = b'{"text":"same"}\n{"text":"same"}\n{"text":"same!"}\n'
            path.write_bytes(data)
            documents = {"1": {"text": "same"}, "2": {"text": "Same!"}}
            freeze = {"documents": 3, "corpus_sha256": hashlib.sha256(data).hexdigest()}

            result = exact_body_overlap(path, documents, freeze)
            self.assertEqual(result["official_documents"], 3)
            self.assertEqual(result["official_matching_documents"], 2)
            self.assertEqual(result["news_documents_with_exact_match"], 1)
            self.assertEqual(result["official_corpus_sha256"], freeze["corpus_sha256"])

            with self.assertRaisesRegex(ValueError, "document count or SHA-256"):
                exact_body_overlap(path, documents, {**freeze, "documents": 2})
            with self.assertRaisesRegex(ValueError, "document count or SHA-256"):
                exact_body_overlap(path, documents, {**freeze, "corpus_sha256": "0" * 64})
