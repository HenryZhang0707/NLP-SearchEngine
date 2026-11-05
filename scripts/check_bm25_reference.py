"""Bounded, optional BM25 reference audit using rank-bm25==0.2.2.

Write generated engineering evidence outside the repository. The reference's
original IDF and its IDF-aligned scores are recorded separately.
"""

import argparse
from collections import Counter
from copy import copy
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform
import sys
import unicodedata

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from contracts import analyzer
from contracts.api import SearchRequest
from offline.build import build
from offline.disk_index import DiskIndex
from online import parser, scorers
from online.engine import SearchEngine


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_case(output, label, rows, queries, reference_cls):
    directory = output / label
    directory.mkdir(exist_ok=True)
    corpus = directory / "corpus.jsonl"
    raw = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode()
    if corpus.exists():
        assert [json.loads(line) for line in corpus.read_text(encoding="utf-8").splitlines()] == rows
    else:
        corpus.write_bytes(raw)
    if not (directory / "index").exists():
        build(corpus, directory / "index", "reference-audit-" + label)
    reader = DiskIndex(directory / "index")
    assert reader.n_docs == len(rows)
    for i, row in enumerate(rows):
        document = reader.doc(i)
        assert (document.doc_id, document.title, document.text) == (row["doc_id"], row["title"], row["text"])
    engine = SearchEngine(reader)
    spec = reader.manifest()["bm25"]
    tokenized = [[token.term for token in analyzer.analyze(row["text"])] for row in rows]
    reference = reference_cls(tokenized, k1=spec["k1"], b=spec["b"])
    library_default = reference_cls(tokenized)
    aligned = copy(reference)
    aligned.idf = {term: math.log(1 + (len(rows) - df + 0.5) / (df + 0.5))
                   for term, df in Counter(term for terms in tokenized for term in set(terms)).items()}
    assert reference.doc_len == [reader.doc_length(i) for i in range(reader.n_docs)]
    assert reference.avgdl == reader.avgdl
    for term in aligned.idf:
        assert reader.df(term) == sum(term in document for document in tokenized)
        assert {docid: tf for docid, tf, _ in reader.postings(term)} == {
            i: document.count(term) for i, document in enumerate(tokenized) if term in document}
    observations = []
    for query in queries:
        try:
            parsed = parser.parse(query)
        except parser.QuerySyntaxError:
            try:
                engine.search(SearchRequest(query=query, mode="lexical", scorer="bm25", expander="none", k=len(rows)))
            except parser.QuerySyntaxError:
                observations.append(dict(query=query, status="EXPECTED_SYNTAX_REJECTION", tokens=[],
                                         aligned_max_abs_error=None, aligned_order=None, original_idf_order=None,
                                         library_default_order=None))
                continue
            raise AssertionError("public engine silently relaxed rejected syntax")
        assert not parsed["explicit_constraints"]
        terms = [token.term for raw in parsed["terms"] for token in analyzer.analyze(raw)]
        candidates = [i for i, document in enumerate(tokenized) if set(terms).intersection(document)]
        current = scorers.bm25(reader, terms)
        expected = aligned.get_scores(terms)
        original_idf = reference.get_scores(terms)
        defaults = library_default.get_scores(terms)
        assert set(current) == set(candidates)
        differences = [abs(current.get(i, 0) - float(expected[i])) for i in range(len(rows))]
        assert max(differences, default=0) <= 1e-12
        ranking = lambda scores: sorted(candidates, key=lambda i: (-float(scores[i]), i))
        expected_order = ranking(expected)
        response = engine.search(SearchRequest(query=query, mode="lexical", scorer="bm25", expander="none", k=len(rows)))
        assert [hit.doc_id for hit in response.results] == [rows[i]["doc_id"] for i in expected_order]
        assert all(math.isclose(hit.score, float(expected[i]), rel_tol=1e-12, abs_tol=1e-12)
                   for i, hit in zip(expected_order, response.results))
        observations.append(dict(query=query, status="SCORING_VERIFIED", tokens=terms, candidate_ids=candidates,
                                 aligned_max_abs_error=max(differences, default=0),
                                 aligned_order=expected_order, original_idf_order=ranking(original_idf),
                                 library_default_order=ranking(defaults),
                                 original_idf_max_abs_difference=max((abs(current.get(i, 0)-float(original_idf[i]))
                                                                    for i in range(len(rows))), default=0)))
    return dict(documents=len(rows), queries=len(queries), corpus_sha256=file_sha(corpus), bm25=spec,
                zero_token_documents=sum(not terms for terms in tokenized),
                scored_queries=sum(item["status"] == "SCORING_VERIFIED" for item in observations),
                syntax_rejections=sum(item["status"] == "EXPECTED_SYNTAX_REJECTION" for item in observations),
                max_aligned_abs_error=max(item["aligned_max_abs_error"] for item in observations if item["status"] == "SCORING_VERIFIED"),
                original_idf_ranking_differences=sum(item["original_idf_order"] != item["aligned_order"]
                                                    for item in observations if item["status"] == "SCORING_VERIFIED"),
                observations=observations)


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--out", required=True, type=Path, help="Fresh output directory outside the repository")
    args = arguments.parse_args()
    try:
        import numpy
        import rank_bm25
        from rank_bm25 import BM25Okapi
    except ImportError:
        arguments.error("optional reference requires: python -m pip install rank-bm25==0.2.2")
    if version("rank-bm25") != "0.2.2":
        arguments.error("reference version must be rank-bm25==0.2.2")
    reference_source = rank_bm25.__loader__.get_source("rank_bm25").encode("utf-8")
    reference_sha = hashlib.sha256(reference_source).hexdigest()
    if reference_sha != "2f28cc795415c01e9f3db5a8ed019774f9cba747b272c9c304271589b8081ac6":
        arguments.error("installed reference source differs from the official 0.2.2 wheel")
    output = args.out.resolve()
    if output.is_relative_to(REPO):
        arguments.error("generated corpora and indexes must be written outside the repository")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    fixture = REPO / "fixtures/tiny_corpus.jsonl"
    rows = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]
    tiny_queries = ["心肌梗死", "胸痛", "患者", "饮食", "高血压 饮食", "发热 胸痛",
                    "不存在的词", "心肌梗死 心肌梗死", "()", "！！！"]
    cases = {"tiny": run_case(output, "tiny", rows, tiny_queries, BM25Okapi)}
    texts = ["shared alpha", "shared alpha alpha", "shared beta", "shared gamma", "shared",
             "shared beta beta", "rare delta 2026", "！！！（）"]
    synthetic = [dict(doc_id=f"reference-edge-{i}", title="", text=text) for i, text in enumerate(texts)]
    cases["edge"] = run_case(output, "edge", synthetic,
                             ["shared", "shared alpha", "alpha alpha", "ALPHA Beta 2026", "rare", "unknown", "！！！"],
                             BM25Okapi)
    record = dict(status="PASS_PINNED_MATURE_BM25_REFERENCE",
                  reference=dict(project="rank-bm25", version="0.2.2", module_sha256=reference_sha,
                                 source="https://pypi.org/project/rank-bm25/0.2.2/"),
                  runtime=dict(python=platform.python_version(), unicode=unicodedata.unidata_version,
                               numpy=numpy.__version__), analyzer_sha256=analyzer.config_hash(),
                  fixture_sha256=file_sha(fixture), audit_code_sha256=file_sha(Path(__file__)),
                  implementation={name: file_sha(REPO/name) for name in
                                  ("contracts/analyzer.py", "online/scorers.py", "online/engine.py")}, cases=cases,
                  comparison_policy="Shared text-only tokenizer, manifest k1/b, union candidates, repeated-query terms, descending score then ascending internal docid.",
                  idf_alignment="Only the reference idf dictionary is replaced with log(1+(N-df+0.5)/(df+0.5)); its score implementation remains unchanged.",
                  scope="Bounded engineering audit; corpus, query counts and syntax rejections are recorded per case.")
    target = output / "receipt.json"
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(dict(status=record["status"], cases={key: {k: v for k, v in value.items() if k != "observations"}
                                                         for key, value in cases.items()},
                          receipt_sha256=file_sha(target)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
