import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation.audit_crawled_dataset import exact_body_overlap, overlap, residual_duplicates
from evaluation.clean_news import TextSignature


class CrawledDatasetAuditTest(unittest.TestCase):
    def test_final_dedup_matches_pairs_across_bit_and_length_boundaries(self):
        fixtures = {
            "original": TextSignature("first", 0, 100),
            "exact": TextSignature("first", 0, 100),
            "near": TextSignature("second", (1 << 32) | (1 << 16) | 1, 90),
            "four_bits": TextSignature("third", (1 << 48) | (2 << 32) | (2 << 16) | 2, 100),
            "too_short": TextSignature("fourth", 0, 80),
        }
        with patch("evaluation.audit_crawled_dataset.signature", side_effect=fixtures.__getitem__):
            result = residual_duplicates(fixtures)
        self.assertEqual(result, {"documents": 5, "normalized_exact_pairs": 1,
                                  "nonexact_near_pairs": 2, "band_candidate_pairs": 6})

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
