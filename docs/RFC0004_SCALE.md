# RFC-0004 scale path

This document describes the optional scale support currently present in the
repository. It is an engineering handoff and does not claim that the course
corpus or the 8M-document build has completed.

## Validated pieces

- `offline.prepare` validates a JSONL corpus, rejects duplicate external IDs,
  writes a normalized corpus and an ID map, and records source/analyzer hashes.
- `offline.compact.compile_index` converts a checksum-verified v1 lexical
  index into a read-only compact artifact. `offline.packed_index.PackedIndex`
  provides bounded decoded-postings caching and a portable positional-read
  fallback on Windows.
- `offline.disk_documents.DiskDocuments` reads one document at a time for
  vector encoding. `adapters.embedding.BGEEncoder` pins the BGE-small-zh
  model revision, tokenizer behavior, dimension, prefix, truncation and
  normalization; `adapters.model_files` verifies every downloaded file.
- `scripts/scale_preflight.py` records source bytes, a bounded token sample,
  raw vector bytes and a planning disk estimate. Its output is explicitly
  `estimate_only`; it does not prove peak RAM, Qdrant overhead or throughput.

The compact reader and the preparation/vector helper pass the tiny-corpus
conformance regression in `tests/test_scale_support.py` on Windows.

## Remaining production gate

The legacy `offline.build` path still materializes the conformance oracle in
memory. The optional compact conversion happens after that build and therefore
does not remove the source peak. `offline.vector_build.QdrantSnapshotWriter`
also retains a Python set of external IDs while a collection is built. These
structures must be replaced by disk-backed uniqueness/length accounting, or
measured with a complete resource receipt, before an 8M build is called
bounded. A Qdrant HNSW result must additionally include server version,
collection metadata, exact point count, snapshot hash, clean-instance restore,
peak RSS/disk, throughput and four-mode evaluator output.

The tiny `scripts/smoke_offline.py` command only imports a snapshot into the
same isolated Qdrant process after deleting its collection. It is a dependency
and wiring smoke test, not clean-instance recovery or HNSW performance
evidence.

Large corpora, model files, Qdrant collections/snapshots, generated indexes,
runs and resource receipts belong outside Git.
