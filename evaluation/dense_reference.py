"""Generate an exact cosine TREC reference from a committed BGE document cache."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import sqlite3
from tempfile import TemporaryDirectory

from adapters.embedding import BGEEncoder, bge_spec
from contracts.index_format import sha256_of
from contracts.retrieval import RetrievalUnavailable, validate_vector
from offline.corpus_documents import CorpusDocuments
from offline.vector_build import _identity_database
from offline.vector_cache import CacheConfig, CachedEncoder
from online import parser as query_parser
from scripts.cache_vectors import bge_cache_contract

from .evaluate import load_queries
from .external_run import _load_exceptions


RUN_TAG = "bge-exact-cosine"


def _topk(np, scores, ordinals, depth):
    """Rank by score descending, then by corpus ordinal ascending."""
    if len(scores) > depth:
        boundary = len(scores) - depth
        cutoff = np.partition(scores, boundary)[boundary]
        higher = np.flatnonzero(scores > cutoff)
        ties = np.flatnonzero(scores == cutoff)
        ties = ties[np.argsort(ordinals[ties], kind="stable")][:depth - len(higher)]
        selection = np.concatenate((higher, ties))
        scores, ordinals = scores[selection], ordinals[selection]
    order = np.lexsort((ordinals, -scores))
    return scores[order], ordinals[order]


def _query_plan(queries, hashes, depth, exceptions):
    records = load_queries(queries)
    if any(record.qid.startswith("#") or any(char.isspace() for char in record.qid)
           for record in records):
        raise ValueError("query IDs must be TREC tokens and may not start with '#'")
    excluded = [record.qid for record in records
                if query_parser.parse(record.query)["explicit_constraints"]]
    policy, policy_sha = _load_exceptions(exceptions, hashes, {r.qid for r in records}, depth)
    if set(excluded) != policy.keys() or any(row["expected_count"] != 0 for row in policy.values()):
        raise ValueError("frozen exceptions must list exactly the explicit-constraint qids at depth zero")
    return records, excluded, policy_sha


def _rank_cache(np, reader, cache, query_vectors, stage, depth, doc_block, query_block):
    dimension = cache.spec.dimension
    document_sha = hashlib.sha256()
    best = [(np.empty(0, dtype=np.float64), np.empty(0, dtype=np.intp))
            for _ in query_vectors]
    with _identity_database(stage) as ids:
        for start in range(0, reader.n_docs, doc_block):
            stop = min(start + doc_block, reader.n_docs)
            docs = [reader.doc(i) for i in range(start, stop)]
            if any(any(char.isspace() for char in doc.doc_id) for doc in docs):
                raise ValueError("document IDs must be TREC tokens")
            try:
                ids.executemany("INSERT INTO ids(doc_id) VALUES (?)", ((doc.doc_id,) for doc in docs))
            except sqlite3.IntegrityError as error:
                raise ValueError("duplicate external document IDs") from error
            raw = np.asarray(cache.encode_documents([doc.title + "\n" + doc.text for doc in docs]),
                             dtype="<f4")
            if raw.shape != (stop - start, dimension) or not np.isfinite(raw).all():
                raise ValueError("invalid cached document vector shape or values")
            document_sha.update(raw.tobytes())
            vectors = raw.astype(np.float64)
            norms = np.linalg.norm(vectors, axis=1)
            if not np.allclose(norms, 1.0, rtol=0, atol=1e-4):
                raise ValueError("cached document vectors violate the unit norm contract")
            vectors /= norms[:, None]
            ordinals = np.arange(start, stop)
            for offset in range(0, len(query_vectors), query_block):
                scores = query_vectors[offset:offset + query_block] @ vectors.T
                if not np.isfinite(scores).all():
                    raise ValueError("nonfinite exact cosine score")
                np.clip(scores, -1.0, 1.0, out=scores)
                for i, row in enumerate(scores, offset):
                    local_scores, local_ids = _topk(np, row, ordinals, depth)
                    prior_scores, prior_ids = best[i]
                    best[i] = _topk(np, np.concatenate((prior_scores, local_scores)),
                                    np.concatenate((prior_ids, local_ids)), depth)
    return best, document_sha.hexdigest()


def generate_run(corpus, queries, output, *, corpus_sha256, n_docs, cache_dir, model_path,
                 depth=100, qrels=None, exceptions=None, device="cpu", chunk_size=50000,
                 batch_size=16, shards=2, doc_block=4096, query_block=32):
    """Use only cached document vectors and the pinned local BGE query encoder."""
    import numpy as np

    for name, value in (("depth", depth), ("doc_block", doc_block), ("query_block", query_block)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if not model_path:
        raise ValueError("a verified local BGE model is required")
    if exceptions is not None and qrels is None:
        raise ValueError("exceptions require qrels for SHA256 binding")
    config = CacheConfig(corpus_sha256, n_docs, bge_spec(), bge_cache_contract(),
                         chunk_size=chunk_size, batch_size=batch_size, shards=shards)
    if depth > n_docs:
        raise ValueError("depth exceeds the number of documents")
    queries, output = Path(queries), Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("output directory already exists")
    inputs = {"queries": queries, "parser": Path(query_parser.__file__), "ranker": Path(__file__)}
    if qrels is not None:
        inputs["qrels"] = Path(qrels)
    if exceptions is not None:
        exceptions = Path(exceptions)
        inputs["exceptions"] = exceptions
    hashes = {"corpus": corpus_sha256, **{key: sha256_of(path) for key, path in inputs.items()}}
    records, excluded, policy_sha = _query_plan(queries, hashes, depth, exceptions)
    if policy_sha != hashes.get("exceptions"):
        raise ValueError("exceptions changed while loading the query plan")
    excluded_set = set(excluded)
    active = [record for record in records if record.qid not in excluded_set]

    output.parent.mkdir(parents=True, exist_ok=True)
    with (CorpusDocuments(corpus, expected_sha256=corpus_sha256, expected_n_docs=n_docs) as source,
          CachedEncoder(cache_dir, source, config) as cache,
          TemporaryDirectory(prefix=f".{output.name}.", dir=output.parent) as staging):
        encoder = BGEEncoder(spec=config.embedding, model_path=str(model_path),
                             device=device, batch_size=batch_size)
        encoded = []
        for record in active:
            vector = encoder.encode_query(record.query)
            validate_vector(vector, config.embedding)
            encoded.append(vector)
        raw_queries = np.asarray(encoded, dtype="<f4").reshape(len(active), config.embedding.dimension)
        query_vectors = raw_queries.astype(np.float64)
        query_vectors /= np.linalg.norm(query_vectors, axis=1, keepdims=True)

        stage = Path(staging)
        best, document_vector_sha = _rank_cache(np, source, cache, query_vectors, stage,
                                                depth, doc_block, query_block)
        run = stage / "run.trec"
        with run.open("x", encoding="utf-8", newline="\n") as handle:
            for record, (scores, ordinals) in zip(active, best):
                if len(scores) != depth:
                    raise ValueError(f"incomplete exact ranking for {record.qid!r}")
                for rank, (score, ordinal) in enumerate(zip(scores, ordinals), 1):
                    doc_id = source.doc(int(ordinal)).doc_id
                    handle.write(f"{record.qid} Q0 {doc_id} {rank} {score:.17g} {RUN_TAG}\n")
        if any(sha256_of(path) != hashes[key] for key, path in inputs.items()):
            raise ValueError("a hashed input changed during scoring")
        manifest = {
            "schema_version": 1, "tag": RUN_TAG, "depth": depth,
            "input_sha256": hashes,
            "cache_identity": config.identity, "cache": config.to_dict(),
            "document_vectors_sha256": document_vector_sha,
            "query_vectors_sha256": hashlib.sha256(raw_queries.tobytes()).hexdigest(),
            "run_sha256": sha256_of(run),
            "documents": n_docs, "queries": len(records), "ranked_queries": len(active),
            "observed_short_queries": dict.fromkeys(excluded, 0),
            "embedding": asdict(config.embedding),
            "scoring": {"method": "float64-cosine", "tie_break": "corpus-ordinal-ascending",
                        "document_block": doc_block, "query_block": query_block},
            "device": device, "python": platform.python_version(),
            "dependencies": {name: version(name) for name in ("numpy", "torch", "transformers")},
        }
        receipt = stage / "manifest.json"
        receipt.write_text(json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                           encoding="utf-8", newline="\n")
        output.mkdir()
        run.rename(output / run.name)
        receipt.rename(output / receipt.name)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("corpus", "queries", "cache-dir", "model-path", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--corpus-sha256", required=True)
    parser.add_argument("--n-docs", type=int, required=True)
    parser.add_argument("--qrels", type=Path, help="hash binding for the frozen exceptions policy")
    parser.add_argument("--exceptions", type=Path, help="zero-depth policy for explicit constraints")
    parser.add_argument("--device", default="cpu")
    for name, default in (("depth", 100), ("chunk-size", 50000), ("batch-size", 16),
                          ("shards", 2), ("doc-block", 4096), ("query-block", 32)):
        parser.add_argument(f"--{name}", type=int, default=default)
    args = parser.parse_args(argv)
    kwargs = vars(args)
    output = kwargs.pop("out")
    try:
        generate_run(output=output, **kwargs)
    except (ImportError, OSError, RetrievalUnavailable, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
