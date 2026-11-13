"""Plan a withdrawal from frozen news20k without modifying or exporting source data."""

import argparse
from collections import Counter
import json
from pathlib import Path
import re

from . import audit_crawled_dataset as audit


DOC_ID = re.compile(r"crawl-[0-9]{6}")
QUERY_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def plan_impact(requested, documents, queries, splits, pairs):
    audit.require(requested and requested <= documents.keys(), "Unknown or empty document selection")
    split_of = {qid: split for split, ids in splits.items() for qid in ids}
    audit.require(set(splits) == {"dev", "test"} and len(split_of) == sum(map(len, splits.values()))
                  and split_of.keys() == queries.keys()
                  and all(QUERY_ID.fullmatch(qid) for qid in queries)
                  and all(row["source_doc_id"] in documents for row in queries.values()),
                  "Invalid query or split identities")
    anchored = {qid for qid, row in queries.items() if row["source_doc_id"] in requested}
    remaining_positives = Counter()
    lost_positives = set()
    direct = via_query = 0
    for (qid, doc_id), grade in pairs.items():
        audit.require(qid in split_of and doc_id in documents and type(grade) is int and grade in (0, 1, 2),
                      "Invalid judged pair")
        if doc_id in requested:
            direct += 1
            if grade and qid not in anchored:
                lost_positives.add(qid)
        elif qid in anchored:
            via_query += 1
        elif grade:
            remaining_positives[qid] += 1
    lost_last = lost_positives - remaining_positives.keys()
    return {
        "anchored_queries": {split: sorted(anchored.intersection(ids)) for split, ids in splits.items()},
        "surviving_queries_losing_last_positive": {
            split: sorted(lost_last.intersection(ids)) for split, ids in splits.items()},
        "removed_pairs": {"direct": direct, "via_removed_query_only": via_query, "total": direct + via_query},
        "remaining_queries": {split: sum(qid not in anchored for qid in ids) for split, ids in splits.items()},
    }


def withdrawal(root, requested):
    audit.require(requested and all(DOC_ID.fullmatch(name) for name in requested), "Invalid document selection")
    crawl = root / "news-clean-20k-20261102"
    receipt = audit.read_json(crawl / "receipt.json", audit.RECEIPT_SHA256)
    attempts = Counter()
    captures = 0

    def visit(row):
        nonlocal captures
        sequence = row["representative_seq"]
        if type(sequence) is int and f"crawl-{sequence:06d}" in requested:
            outcome = row["outcome"]
            audit.require(outcome in ("success", "duplicate_exact", "duplicate_near"),
                          "Unexpected linked crawl outcome")
            attempts[outcome] += 1
            captures += len(row["captures"])

    documents, _ = audit.corpus_stats(crawl, receipt, visit_sidecar=visit)
    queries, splits, query_hash = audit.query_sets(root / "news-query-design-20261102", documents)
    _, pairs = audit.qrel_stats(
        root / "news-formal-pooled-qrels-20261104",
        root / "news-formal-http-pool-rm3-resume-20261103" / "pool",
        queries, splits, documents, query_hash, receipt["corpus_sha256"], all_pairs=True)
    impact = plan_impact(requested, documents, queries, splits, pairs)
    audit.require(attempts["success"] == len(requested), "Missing source attempt for selected document")
    return {
        "status": "DRY_RUN_ONLY",
        "source": {"crawl_receipt_sha256": audit.RECEIPT_SHA256,
                   "query_manifest_sha256": audit.SELECTED_MANIFEST_SHA256,
                   "qrels_manifest_sha256": audit.QRELS_MANIFEST_SHA256},
        "document_ids": sorted(requested),
        "crawl_attempts": dict(sorted(attempts.items())),
        "raw_capture_references": captures,
        **impact,
        "invalidate": ["lexical_and_dense_indexes", "retrieval_runs", "candidate_pool", "qrels",
                       "quality_and_latency_reports", "release_and_handoff_artifacts"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Authorized frozen evidence directory")
    parser.add_argument("--doc-id", action="append", required=True, help="Stable ID; repeat to plan a batch")
    args = parser.parse_args()
    try:
        result = withdrawal(args.root, set(args.doc_id))
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(2, "Withdrawal plan could not verify the frozen inputs or document selection.\n")
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
