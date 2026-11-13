"""Generate an independent, formula-aligned BM25S TREC run."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
from tempfile import TemporaryDirectory
import unicodedata

from contracts import analyzer
from contracts.index_format import BM25Spec
from contracts.retrieval import corpus_fingerprint
from offline.corpus_documents import CorpusDocuments
from offline.disk_index import DiskIndex
from online import parser as query_parser
from online.scorers import boolean_docs

from .evaluate import EvaluationError, _sha256, load_queries


BM25S_VERSION = "0.2.14"
RUN_TAG = "bm25s-atire-lucene"


def generate_run(corpus, queries, output, *, corpus_sha256, n_docs,
                 depth=100, k1=1.2, b=0.75, constraint_index=None):
    """Score with BM25S and share the query syntax and hard-filter contract."""
    if type(depth) is not int or depth < 1:
        raise ValueError("depth must be a positive integer")
    spec = BM25Spec(k1=k1, b=b)
    output = Path(output)
    if output.exists():
        raise ValueError("output directory already exists")
    if version("bm25s") != BM25S_VERSION:
        raise ValueError(f"install bm25s=={BM25S_VERSION} for the reference run")
    from bm25s import BM25
    import numpy as np

    queries_sha256 = _sha256(queries)
    records = load_queries(queries)
    parsed = [(record.qid, query_parser.parse(record.query)) for record in records]
    if any(qid.startswith("#") or any(char.isspace() for char in qid) for qid, _ in parsed):
        raise ValueError("query IDs must be TREC tokens and may not start with '#'")
    requires_filter = any(query["explicit_constraints"] for _, query in parsed)
    if requires_filter and constraint_index is None:
        raise ValueError("explicit queries require --constraint-index for hard filtering")

    with ExitStack() as stack:
        source = stack.enter_context(CorpusDocuments(
            corpus, expected_sha256=corpus_sha256, expected_n_docs=n_docs))
        fingerprint = corpus_fingerprint(source)
        reader = None
        if requires_filter:
            reader = DiskIndex(constraint_index)
            close = getattr(reader, "close", None)
            if callable(close):
                stack.callback(close)
            if corpus_fingerprint(reader) != fingerprint:
                raise ValueError("constraint index must have the same ordered corpus")
        ids = []
        tokenized = []
        vocab = {}
        for docid in range(n_docs):
            doc = source.doc(docid)
            if any(char.isspace() for char in doc.doc_id):
                raise ValueError(f"document ID at row {docid + 1} contains TREC delimiters")
            ids.append(doc.doc_id)
            tokenized.append([vocab.setdefault(token.term, len(vocab)) for token in analyzer.analyze(doc.text)])
        if len(set(ids)) != n_docs:
            raise ValueError("duplicate external document IDs")

        reference = BM25(k1=spec.k1, b=spec.b, method="atire",
                         idf_method="lucene", dtype="float64", backend="numpy")
        if vocab:
            # A tuple of integer tokens and their vocabulary also supports a
            # zero-token first document in BM25S 0.2.14.
            reference.index((tokenized, vocab), create_empty_token=False, show_progress=False)
        del tokenized

        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=f".{output.name}.", dir=output.parent) as staging:
            stage = Path(staging)
            run = stage / "run.trec"
            short_queries = {}
            constrained = []
            with run.open("x", encoding="utf-8", newline="\n") as handle:
                for qid, query in parsed:
                    terms = [token.term for raw in query["terms"] for token in analyzer.analyze(raw)]
                    token_ids = [vocab[term] for term in terms if term in vocab]
                    scores = reference.get_scores_from_ids(token_ids) if token_ids else np.zeros(n_docs)
                    if query["explicit_constraints"]:
                        constrained.append(qid)
                        eligible = np.array(sorted(boolean_docs(reader, query["ast"], analyzer.analyze)),
                                            dtype=np.intp)
                    else:
                        eligible = np.flatnonzero(scores > 0.0)
                    order = np.argsort(-scores[eligible], kind="stable")[:depth]
                    if len(order) < depth:
                        short_queries[qid] = len(order)
                    for rank, docid in enumerate(eligible[order], 1):
                        handle.write(f"{qid} Q0 {ids[docid]} {rank} {scores[docid]:.17g} {RUN_TAG}\n")
            if _sha256(queries) != queries_sha256:
                raise ValueError("queries changed during the reference run")
            manifest = {
                "schema_version": 1, "tag": RUN_TAG, "depth": depth,
                "corpus_sha256": corpus_sha256, "corpus_fingerprint": fingerprint,
                "queries_sha256": queries_sha256, "run_sha256": _sha256(run),
                "documents": n_docs, "queries": len(parsed),
                "hard_filtered_queries": constrained, "observed_short_queries": short_queries,
                "bm25": spec.to_dict(), "analyzer_sha256": analyzer.config_hash(),
                "dependencies": {name: version(name) for name in ("bm25s", "numpy", "scipy")},
                "python": platform.python_version(), "unicode": unicodedata.unidata_version,
            }
            staged_manifest = stage / "manifest.json"
            staged_manifest.write_text(json.dumps(manifest, indent=2) + "\n",
                                       encoding="utf-8", newline="\n")
            output.mkdir()
            run.rename(output / run.name)
            staged_manifest.rename(output / staged_manifest.name)
    return manifest


def main(argv=None):
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--corpus", type=Path, required=True)
    args.add_argument("--corpus-sha256", required=True)
    args.add_argument("--n-docs", type=int, required=True)
    args.add_argument("--queries", type=Path, required=True)
    args.add_argument("--out", type=Path, required=True)
    args.add_argument("--depth", type=int, default=100)
    args.add_argument("--k1", type=float, default=1.2)
    args.add_argument("--b", type=float, default=0.75)
    args.add_argument("--constraint-index", type=Path)
    opts = args.parse_args(argv)
    try:
        generate_run(opts.corpus, opts.queries, opts.out, corpus_sha256=opts.corpus_sha256,
                     n_docs=opts.n_docs, depth=opts.depth, k1=opts.k1, b=opts.b,
                     constraint_index=opts.constraint_index)
    except PackageNotFoundError:
        args.error(f"install bm25s=={BM25S_VERSION} for the reference run")
    except (EvaluationError, OSError, ValueError) as error:
        args.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
