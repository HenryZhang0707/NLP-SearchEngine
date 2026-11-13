import unittest

from evaluation.news_withdrawal import plan_impact


class NewsWithdrawalImpactTest(unittest.TestCase):
    def setUp(self):
        self.anchor = "crawl-000001"
        self.other_anchor = "crawl-000002"
        self.retained = "crawl-000003"
        self.unpooled = "crawl-000004"
        self.documents = {
            doc_id: {"doc_id": doc_id}
            for doc_id in (self.anchor, self.other_anchor, self.retained, self.unpooled)
        }
        self.queries = {
            "q-dev": {"source_doc_id": self.anchor},
            "q-test": {"source_doc_id": self.other_anchor},
        }
        self.splits = {"dev": ["q-dev"], "test": ["q-test"]}
        self.pairs = {
            ("q-dev", self.anchor): 2,
            ("q-dev", self.retained): 0,
            ("q-test", self.anchor): 1,
            ("q-test", self.other_anchor): 2,
        }

    def impact(self, *doc_ids):
        return plan_impact(set(doc_ids), self.documents, self.queries, self.splits, self.pairs)

    def test_anchor_withdrawal_counts_overlapping_pair_only_once(self):
        result = self.impact(self.anchor)
        self.assertEqual(result["anchored_queries"], {"dev": ["q-dev"], "test": []})
        self.assertEqual(
            result["removed_pairs"], {"direct": 2, "via_removed_query_only": 1, "total": 3}
        )
        self.assertEqual(result["remaining_queries"], {"dev": 0, "test": 1})
        self.assertEqual(
            result["surviving_queries_losing_last_positive"], {"dev": [], "test": []}
        )

    def test_batch_reports_surviving_query_that_loses_its_only_positive(self):
        self.pairs[("q-test", self.other_anchor)] = 0
        result = self.impact(self.anchor, self.retained)
        self.assertEqual(
            result["removed_pairs"], {"direct": 3, "via_removed_query_only": 0, "total": 3}
        )
        self.assertEqual(
            result["surviving_queries_losing_last_positive"], {"dev": [], "test": ["q-test"]}
        )

    def test_unpooled_document_has_no_direct_or_query_impact(self):
        result = self.impact(self.unpooled)
        self.assertEqual(result["anchored_queries"], {"dev": [], "test": []})
        self.assertEqual(
            result["removed_pairs"], {"direct": 0, "via_removed_query_only": 0, "total": 0}
        )
        self.assertEqual(result["remaining_queries"], {"dev": 1, "test": 1})

    def test_unknown_document_rejects_whole_request(self):
        with self.assertRaises(ValueError):
            self.impact(self.anchor, "crawl-099999")


if __name__ == "__main__":
    unittest.main()
