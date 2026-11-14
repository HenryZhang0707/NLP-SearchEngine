"""Generate an exact cosine TREC reference from a committed BGE document cache."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, replace
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from multiprocessing import get_context
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
FAISS_VERSION = "1.10.0"
FAISS_RUN_TAG = "bge-faiss-flat-ip"
# FAISS 1.10 switches to BLAS at 20 queries. Keep both the first search and
# the single-row tie search on the same FP32 accumulation path.
FAISS_DIRECT_QUERY_LIMIT = 19


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


def _document_blocks(np, reader, cache, stage, doc_block, document_sha):
    dimension = cache.spec.dimension
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
            yield np.arange(start, stop), vectors


def _rank_cache(np, blocks, query_vectors, depth, query_block):
    best = [(np.empty(0, dtype=np.float64), np.empty(0, dtype=np.intp))
            for _ in query_vectors]
    for ordinals, vectors in blocks:
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
    return best


def _rank_faiss(np, faiss, blocks, query_vectors, depth, query_block):
    index = faiss.IndexFlatIP(query_vectors.shape[1])
    for _, vectors in blocks:
        index.add(vectors.astype(np.float32))
    n_docs = index.ntotal
    search_depth = min(depth + 1, n_docs)
    query_vectors = query_vectors.astype(np.float32)
    best = []
    for offset in range(0, len(query_vectors), query_block):
        queries = query_vectors[offset:offset + query_block]
        scores, ordinals = index.search(queries, search_depth)
        for i, (row, ids) in enumerate(zip(scores, ordinals)):
            if search_depth > depth and search_depth < n_docs and row[depth - 1] == row[depth]:
                all_scores, all_ids = index.search(queries[i:i + 1], n_docs)
                row, ids = all_scores[0], all_ids[0]
            if not np.isfinite(row).all() or ((ids < 0) | (ids >= n_docs)).any():
                raise ValueError("invalid FAISS exact search results")
            best.append(_topk(np, row, ids, depth))
    return best


def _load_faiss():
    try:
        installed = version("faiss-cpu")
    except PackageNotFoundError as error:
        raise ImportError(f"--backend faiss requires faiss-cpu=={FAISS_VERSION}") from error
    if installed != FAISS_VERSION:
        raise ValueError(f"--backend faiss requires faiss-cpu=={FAISS_VERSION}, found {installed}")
    import faiss

    if faiss.__version__ != installed:
        raise ValueError("imported FAISS version does not match the faiss-cpu distribution")
    return faiss, installed


def _encode_queries(records, spec, model_path, device, batch_size):
    import numpy as np

    encoder = BGEEncoder(spec=spec, model_path=str(model_path), device=device, batch_size=batch_size)
    encoded = []
    for record in records:
        vector = encoder.encode_query(record.query)
        validate_vector(vector, spec)
        encoded.append(vector)
    return np.asarray(encoded, dtype="<f4").reshape(len(records), spec.dimension)


def _encode_faiss_queries(records, spec, model_path, device, batch_size):
    # Windows wheels for FAISS and PyTorch can load incompatible OpenMP
    # libraries. A fresh process keeps PyTorch out of the ranking process.
    try:
        with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as worker:
            return worker.submit(_encode_queries, records, spec, model_path, device, batch_size).result()
    except BrokenProcessPool as error:
        raise RetrievalUnavailable("The BGE query encoding process stopped unexpectedly") from error


def _admit_reference_manifest(reference, config, hashes):
    path, expected_sha = reference
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("reference manifest SHA256 mismatch")
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1 or manifest.get("tag") != RUN_TAG
            or not isinstance(manifest.get("scoring"), dict)
            or manifest["scoring"].get("method") != "float64-cosine"):
        raise ValueError("reference manifest must describe a frozen NumPy exact run")
    reference_hashes = manifest.get("input_sha256")
    if (not isinstance(reference_hashes, dict)
            or {k: v for k, v in reference_hashes.items() if k != "ranker"}
            != {k: v for k, v in hashes.items() if k != "ranker"}):
        raise ValueError("reference manifest input SHA256 mismatch")
    cache = manifest.get("cache")
    if not isinstance(cache, dict):
        raise ValueError("reference manifest is missing the cache configuration")
    historical = replace(config, encoder_contract=cache.get("encoder_contract"))
    if (historical.identity != manifest.get("cache_identity")
            or json.dumps(historical.to_dict(), sort_keys=True) != json.dumps(cache, sort_keys=True)):
        raise ValueError("reference manifest cache identity or configuration mismatch")
    for name in ("document_vectors_sha256", "query_vectors_sha256"):
        digest = manifest.get(name)
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise ValueError(f"reference manifest has an invalid {name}")
    return historical, manifest


def generate_run(corpus, queries, output, *, corpus_sha256, n_docs, cache_dir, model_path,
                 depth=100, qrels=None, exceptions=None, device="cpu", chunk_size=50000,
                 batch_size=16, shards=2, doc_block=4096, query_block=32, backend="numpy",
                 reference_manifest=None):
    """Use only cached document vectors and the pinned local BGE query encoder."""
    import numpy as np

    for name, value in (("depth", depth), ("doc_block", doc_block), ("query_block", query_block)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if backend not in ("numpy", "faiss"):
        raise ValueError("backend must be numpy or faiss")
    if reference_manifest is not None and backend != "faiss":
        raise ValueError("--reference-manifest requires --backend faiss")
    tag = RUN_TAG
    scoring = {"method": "float64-cosine", "tie_break": "corpus-ordinal-ascending",
               "document_block": doc_block, "query_block": query_block}
    if backend == "faiss":
        faiss, faiss_version = _load_faiss()
        query_block = min(query_block, FAISS_DIRECT_QUERY_LIMIT,
                          faiss.cvar.distance_compute_blas_threshold - 1)
        if query_block < 1:
            raise ValueError("FAISS direct inner-product search is disabled")
        tag = FAISS_RUN_TAG
        scoring.update(method="faiss-index-flat-ip-float32", query_block=query_block,
                       normalization="float64-l2-then-float32")
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
    if reference_manifest is not None:
        config, reference = _admit_reference_manifest(reference_manifest, config, hashes)
    records, excluded, policy_sha = _query_plan(queries, hashes, depth, exceptions)
    if policy_sha != hashes.get("exceptions"):
        raise ValueError("exceptions changed while loading the query plan")
    excluded_set = set(excluded)
    active = [record for record in records if record.qid not in excluded_set]

    output.parent.mkdir(parents=True, exist_ok=True)
    with (CorpusDocuments(corpus, expected_sha256=corpus_sha256, expected_n_docs=n_docs) as source,
          CachedEncoder(cache_dir, source, config) as cache,
          TemporaryDirectory(prefix=f".{output.name}.", dir=output.parent) as staging):
        encode = _encode_faiss_queries if backend == "faiss" else _encode_queries
        raw_queries = encode(active, config.embedding, model_path, device, batch_size)
        if (raw_queries.dtype != np.dtype("<f4") or raw_queries.shape != (len(active), config.embedding.dimension)
                or not np.isfinite(raw_queries).all()):
            raise ValueError("invalid encoded query vectors")
        query_vector_sha = hashlib.sha256(raw_queries.tobytes()).hexdigest()
        if reference_manifest is not None and query_vector_sha != reference["query_vectors_sha256"]:
            raise ValueError("query vector SHA256 differs from the reference manifest")
        query_vectors = raw_queries.astype(np.float64)
        query_vectors /= np.linalg.norm(query_vectors, axis=1, keepdims=True)

        stage = Path(staging)
        document_sha = hashlib.sha256()
        blocks = _document_blocks(np, source, cache, stage, doc_block, document_sha)
        if backend == "faiss":
            best = _rank_faiss(np, faiss, blocks, query_vectors, depth, query_block)
        else:
            best = _rank_cache(np, blocks, query_vectors, depth, query_block)
        document_vector_sha = document_sha.hexdigest()
        if reference_manifest is not None and document_vector_sha != reference["document_vectors_sha256"]:
            raise ValueError("document vector SHA256 differs from the reference manifest")
        run = stage / "run.trec"
        with run.open("x", encoding="utf-8", newline="\n") as handle:
            for record, (scores, ordinals) in zip(active, best):
                if len(scores) != depth:
                    raise ValueError(f"incomplete exact ranking for {record.qid!r}")
                for rank, (score, ordinal) in enumerate(zip(scores, ordinals), 1):
                    doc_id = source.doc(int(ordinal)).doc_id
                    handle.write(f"{record.qid} Q0 {doc_id} {rank} {score:.17g} {tag}\n")
        if any(sha256_of(path) != hashes[key] for key, path in inputs.items()):
            raise ValueError("a hashed input changed during scoring")
        manifest = {
            "schema_version": 1, "tag": tag, "depth": depth,
            "input_sha256": hashes,
            "cache_identity": config.identity, "cache": config.to_dict(),
            "document_vectors_sha256": document_vector_sha,
            "query_vectors_sha256": query_vector_sha,
            "run_sha256": sha256_of(run),
            "documents": n_docs, "queries": len(records), "ranked_queries": len(active),
            "observed_short_queries": dict.fromkeys(excluded, 0),
            "embedding": asdict(config.embedding),
            "scoring": scoring,
            "device": device, "python": platform.python_version(),
            "dependencies": {name: version(name) for name in ("numpy", "torch", "transformers")},
        }
        if backend == "faiss":
            manifest["dependencies"]["faiss-cpu"] = faiss_version
        if reference_manifest is not None:
            manifest["reference_manifest_sha256"] = reference_manifest[1]
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
    parser.add_argument("--backend", choices=("numpy", "faiss"), default="numpy",
                        help=f"optional FAISS IndexFlatIP backend requires faiss-cpu=={FAISS_VERSION}")
    parser.add_argument("--reference-manifest", nargs=2, metavar=("PATH", "SHA256"),
                        help="bind historical cache identity and vector bytes to a frozen NumPy manifest")
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
