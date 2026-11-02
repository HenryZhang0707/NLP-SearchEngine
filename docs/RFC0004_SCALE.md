# RFC-0004 scale path

This document describes the optional scale support currently present in the
repository. It is an engineering handoff and does not claim that the course
corpus or the 8M-document build has completed.

## Validated pieces

- `offline.prepare` validates a JSONL corpus, rejects duplicate external IDs,
  writes a normalized corpus and an ID map, and records source/analyzer hashes.
- `offline.stream_build` uses SQLite-backed uniqueness, length, and posting
  storage and streams the unchanged v1 JSONL output. It removes the legacy
  builder's full Python conformance oracle from the lexical build path.
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

## Full-corpus continuation (2026-11-02)

The official archive was resumed, verified against SHA256
`b051d3d966f7b0288aed95a923fc3b706176767d8bdcff8aa8fe5fff84b20662`,
and prepared locally in an atomic staging directory. The published corpus has
8,096,668 documents, 2,000 official dev queries and 9,839 qrels; its 7,425,979,008
bytes hash to `bc67f3ccfe7423c039f04c7a5e7f6eae07a7f722087edf0caa7cfc694a8c45e8`.
Preparation retains official order and external IDs. Redundant full-size ID
sets were removed, and late conversion errors now leave no published output.

The official corpus includes nonempty punctuation-only text such as `()`.
The lexical contract now explicitly preserves these documents with zero token
length, empty offsets and no postings. Both mixed and all-zero-token corpora
pass the exact analyzer oracle; no documents are removed to make a build pass.

For large conversion, install the optional `ijson==3.4.0.post0` extra and use:

```bash
python -m offline.stream_build --corpus /data/corpus.jsonl \
  --out /output/lexical --work-dir /scratch/sqlite --dataset official-dev \
  --progress-every 10000
python -m offline.compact --source /output/lexical --out /output/compact --streaming
python -m unittest tests.test_compact_stream -v
```

`--work-dir` separates SQLite scratch from the atomically published lexical
directory; it is not a checkpoint/resume flag. `--streaming` consumes posting
events and writes 16,384 uint32 values per buffer, instead of materializing an
entire high-df JSON object. Lengths/totals use compact arrays and external-ID
uniqueness uses a temporary SQLite table. Both compilers require valid SHA256
values for every source artifact. The default converter remains dependency free.
Streaming preserves compact-v1; tiny outputs match the default converter byte
for byte, and malformed/truncated data, forged zeros and null checksums fail.

A separate 10,000-document resource pilot from official shard 01 uses synthetic
IDs and is not evaluation data. It retained the punctuation-only record:

| Stage | Seconds | Process peak working set | Result |
| --- | ---: | ---: | --- |
| SQLite stream build | 152.93 | 33,714,176 bytes | 10,000 docs |
| Event-stream compact | 21.24 | 34,058,240 bytes | compact-v1 |
| Stream repeat for disk measurement | 106.67 | 33,710,080 bytes | identical lexical file hashes |

The disk repeat sampled every 100 ms: SQLite peak 42,618,880 bytes and total
staging peak 130,210,588 bytes. It measures a previously missing resource cost;
it is not another retrieval-quality experiment. Full-scale throughput and memory
remain unproven by this pilot. Planning uses a 50% disk margin plus 10 GiB of
headroom per output/scratch volume.

The local full-corpus job uses a frozen source copy, separate output/scratch
volumes, and external heartbeat/phase receipts. It must finish the lexical stage
before starting compact; neither an in-progress directory nor a launch receipt
counts as a completed 8M artifact. The remaining reader acceptance and full-scale
Qdrant/HNSW experiments are still required. Historical IAB task navigation
reported `node-service` not ready; the latest IAB session/navigation also failed,
and another entry returned an empty response. These observations are limited to
those access attempts: a local terminal received HTTP 200 with a normal page
title from one entry, and the user confirmed their own browser works. They do
not establish cluster unavailability or insufficient quota. A memory-only API
session subsequently authenticated, read existing jobs and remaining CPU/RAM/GPU
quotas, and submitted an isolated CPU-only source preflight. It passed core,
evaluation, 56 targeted tests and compilation in 52.90 seconds on Python 3.10.14
and Torch 2.3.0, using 2 CPUs / 2 GiB host RAM / 0 GPUs. No existing cluster
task was changed or new GPU job submitted. The user prohibits computer-control
access to the cluster because it takes over the keyboard/mouse; further access
must use non-GUI CLI/SSH/API, with task/resource state verified before submission.

### Process-termination recovery correction

The initial full-corpus process subsequently disappeared without a completion
receipt. Its journal-OFF SQLite staging database failed a read-only query with
`database disk image is malformed`. The old directories and launch state are
preserved as failure evidence and are not accepted as resumable checkpoints.

The new `offline.checkpoint_build` entry binds full input SHA256/size, dataset,
BM25, analyzer, runtime and code hashes. It uses an OS state lock and SQLite
WAL/FULL. Each ingestion checkpoint fsyncs docs/offsets before committing rows,
input byte offset, file lengths and document count in one transaction. Resume
truncates uncommitted file tails and replays from the committed input offset.
Postings export checkpoints complete-term boundaries every 1000 terms or
16 MiB; file hashes are committed individually. A partial term or unfinished
single-file hash is redone. Stats export is still a whole-file replay unit.

```bash
python -m offline.checkpoint_build --corpus /data/corpus.jsonl \
  --out /output/lexical --state-dir /scratch/checkpoint \
  --dataset official-dev --checkpoint-docs 1000 --progress-every 1000
python -m unittest tests.test_checkpoint_build -v
```

Use the same command and frozen code to resume after verifying that its prior
process is terminal. This is a new state format and cannot adopt old stream
builder databases. The subprocess/fault tests cover hard exits around file
fsync, SQL commit, partial term export, hashes and publication, with byte equality
to the original stream builder. They establish process-crash recovery; Windows
power-loss and disk-failure tolerance are not claimed. The restarted full-corpus
job currently runs only the lexical stage; full-scale compact and reader acceptance
remain subsequent work. The frozen runtime of an active job must not be replaced
by later source fixes; resume it using its original binding. A reviewed bootstrap
repair admits only a provably empty, never-committed SQLite database without an
existing stage/target or unknown state files. Damaged or nonempty state remains
protected and fails closed.

### Resumable compact and online consumption

```bash
python -m offline.checkpoint_compact --source /output/lexical \
  --out /output/compact --state-dir /scratch/compact-checkpoint \
  --checkpoint-docs 1000
python -m online.server --index /output/compact --port 8000
python -m unittest tests.test_checkpoint_build tests.test_checkpoint_compact \
  tests.test_compact_stream -v
```

This optional ijson entry uses only the verified public v1 files, not the
lexical builder database. Its phases cover source verification, docs, offsets,
bounded JSONL row scanning, term-boundary postings, terms SQLite export, hashes
and atomic publication. File fsync precedes checkpoint commits. Input/config/code
binding, OS locking, output truncation and replay preserve already committed work.
Single documents/terms and unfinished file hashes remain replay units. Completed
SHA caches require matching size and mtime; final reader admission still hashes
all compact artifact files independently.

On the same real 10k resource pilot, all six docs/binary files match the previous
streaming converter byte for byte, all 384,350 term rows match logically, and 28
deterministic document samples pass text/length/offset/position checks through
the normal `DiskIndex` entry returning `PackedIndex`. A post-build validation
wrapper initially used a nonexistent reader method; corrected acceptance passed
without rebuilding or altering the artifact. That wrapper did not save conversion
time/peak RAM before failing, so no new cost measurement is claimed.

Compact auto-detection keeps the existing online import boundary. A directory
with both manifest formats is rejected, and the legacy checksum-disable argument
cannot bypass compact checks. Source schema/statistics/checksum metadata and
average length are also validated. This 10k acceptance is not an 8M receipt.

As an external, non-versioned smoke receipt, the same path was run on the
1,029-document depth-4 government-web crawl: `stream_build` completed in
66.69 seconds with average document length 2,945.7, and `compact` completed in
15.22 seconds with 178,504 terms. The resulting manifests and their hashes are
recorded in `evaluation/RESOURCES.md`. This is evidence that the staged path is
usable on a small external corpus; it is not an 8M peak-RSS, disk, throughput,
or Qdrant HNSW receipt.

The depth-4 corpus was then merged by URL with 108 pages fetched from the
site's public sitemap batch, producing 1,137 documents. The merged stream build
completed in 74.19 seconds with average document length 2,797.2; compact
conversion completed in 13.97 seconds with 186,263 terms. The merged manifest
and compact hash are recorded in `evaluation/RESOURCES.md`. These measurements
remain bounded engineering smoke evidence, not an 8M resource receipt.

A separate `contracts.conformance.check_reader` attempt against the 1,029-document
packed artifact exceeded 20 minutes of local CPU time without producing a
receipt and was stopped. The tiny packed-reader conformance remains green; the
large conformance path is therefore unvalidated and must be bounded before it
is used as a production gate.

## Pinned BGE serving gate

`online.server --bge-model-path /models/verified-bge --encoder-device cpu`
reuses the same shared encoder as `scripts/build_vectors.py`; on the cluster
choose the allocated CUDA device explicitly. The local inventory is checked
before loading, forward weights are explicitly FP32, pooling is CLS, tokenizer
truncation is right-sided and normalization uses float32. The default generic
SentenceTransformer path stays compatible. Inapplicable options, missing optional
dependencies, wrong model identity and invalid local inventory fail clearly.
This wiring is covered by stub-only tests; before the full semantic build, a
real-model smoke must record runtime/dtype and check short/long inputs, padding,
batch invariance, finite values, norms and the actual service encoder path. The
stub-only tests are not evidence of real-model equivalence or semantic quality.

The first real CPU admission attempt checked the old model cache and stopped
before dependency installation or inference: none of the bounded candidate
directories contained all six pinned files. This is a missing-cache result,
not evidence that the cluster is unavailable. A separate fresh stage downloads
only the official pinned files and checks their known SHA256 values before the
same frozen numerical gate. The direct cluster download timed out; the six
official files were instead downloaded locally and transported with verified
remote-download hashes. A native Linux wheelhouse then verified 34 wheels
against official PyPI metadata and dependency closure. Its 46,498,720-byte archive
hashes to `d39dc737917e17ebab4f23a0085170709a6e715eca5750915a9bff4d57dac277`.

The real CPU gate now passes on Python 3.10.14, Torch 2.3.0, Transformers 4.46.3,
tokenizers 0.20.3 and the pinned six-file model. It checks actual FP32 forward,
CLS plus float32 L2, right/512 truncation, short/punctuation/long inputs,
batch/padding, shared query/document instances and repeat encoding. The largest
absolute difference is `1.775333657860756e-7`, below the frozen `1e-5` tolerance;
norms range from 0.9999999454 to 1.0000000735. Process peak RSS is 743,702,528 bytes.
The 211.51-second worker includes 147.69 seconds of offline dependency installation.
Receipt SHA256:
`25c9369d437f07265ef2e90a51383dfb1e7f35c55843827d34aa9d984e54ae93`.
This is a real numerical admission, not retrieval-quality evidence. GPU numerical
and throughput admission remain separate from CPU success.

`scripts/build_vectors.py` also accepts `--corpus` with mandatory
`--corpus-sha256` and `--corpus-n-docs`, mutually exclusive with `--index`.
`CorpusDocuments` scans and verifies immutable raw JSONL before model loading,
keeps two uint64 offset/length arrays, and preserves physical row IDs and all
original fields. It rejects blank lines and observable input changes. The
existing vector builder still checks external-ID uniqueness and computes the
unchanged ordered fingerprint before publication. It creates no lexical files
and does not relax final dual-index fingerprint matching.

This document source has now passed a real full-input admission, independently
of the unfinished lexical build: it scanned and parsed all 8,096,668 rows of the
7,425,979,008-byte frozen corpus, verified the expected raw SHA256, and read fixed
internal IDs 0, 4,048,334 and 8,096,667. Construction took 108.55 seconds, with a
process peak working set of 218,562,560 bytes (208.44 MiB), below the predeclared
256 MiB limit. The two uint64 arrays contain 129,546,688 payload bytes. The
supervisor observed a clean exit and a closed reader, and source hashes matched
before and after the run. The admission receipt SHA256 is
`12784e7d2156c756f0962eb867c8564278b7153994b26f7e6a10bec3bb558491`.
This proves full-input document access and its measured memory bound; external-ID
uniqueness, final dual-index alignment, semantic encoding and HNSW delivery retain
their separate gates.

The serving CLI can pass `--hnsw-ef 256` through to Qdrant's nonexact search
parameters. This explicit configuration has SDK-double regression coverage;
actual graph construction and planner behavior still require a real service.

## Vector publication ownership

Vector identity databases now live in per-invocation temporary directories.
Existing reserved SQLite files and snapshot/manifest temporary files are never
adopted or removed. Snapshot downloads use exclusive `.part` creation, validate
an integer server-declared size when supplied, hash the complete bytes, and
publish with an atomic non-overwriting hard link. Manifest publication uses the
same non-overwriting mechanism. Both require hard-link support in the output
filesystem and fail closed otherwise; this is not a power-loss durability claim.
Snapshot filenames reject both POSIX and Windows traversal and reserved names.

A collection preflight 404 does not reserve its name. Failed, conflicting,
explicitly rejected or timed-out creation never grants cleanup ownership. Only
an acknowledged creation may be cleaned after later validation failure. A lost
success response can therefore leave an unpublished collection for inspection;
unknown ownership is preserved rather than inferred. Fifteen targeted
regressions cover these filesystem and remote-creation boundaries.

## Real REST validation correction

The official Qdrant 1.16.0 binary rejected `full_scan_threshold=0` with HTTP422,
requiring at least 10 KiB. Its REST `HnswConfigDiff` also requires
`ef_construct>=4`; these limits differ from the lower-level core config. The
writer and CLI now enforce them before remote creation or model loading. The
old frozen recipe is preserved and explicitly superseded by a 10-KiB recipe
before formal quality requests; no retrieval result informed this correction.
The negative probe stopped because it had expected HTTP400; its positive
case and graph behavior were not executed. A subsequent independent pipeline
verified real HTTP200 creation at10KiB, metadata, exact zero count and YAML
inheritance before model loading. Its identity gates also passed, but model
initialization then exhausted the bounded budget (1185.58s wall,11.84s CPU);
encoding, graph search and clean restore remain unverified. The supervisor
cleaned only its new process group within1193.16s. A separate10k full-builder
probe stopped before its worker because cgroup accounting paths were unresolved;
its43.44s is not build-throughput evidence.

The shared private filesystem was detected as NFS. Qdrant data must use an
isolated supported runtime filesystem. Container `/tmp` reports overlay; that
alone does not prove a physical-disk writable backing or admit an 8M build.
YAML configuration discovery and non-default port/config checks are mandatory;
a silently ignored JSON config is a failed setup, not an accepted downgrade.

## Remaining production gate

The current code batch passes core 62/62, evaluation 74/74,
checkpoint/stream/BGE/document-source/CLI/HNSW/artifact-ownership checks 107/107, Node 5/5,
compilation and diff checks.
Independent final review found no additional blocker. These checks do not
replace full-index acceptance, the real-model smoke or HNSW experiments.

The legacy `offline.build` path still materializes the conformance oracle in
memory; use `offline.stream_build` when measuring a large lexical build. The
optional compact conversion happens after a source build and therefore does
not remove that source peak. `offline.vector_build.QdrantSnapshotWriter` now
uses a temporary SQLite identity table; the 8M vector build still needs a
complete resource receipt before it is called bounded. A Qdrant HNSW result
must additionally include server version,
collection metadata, exact point count, snapshot hash, clean-instance restore,
peak RSS/disk, throughput and four-mode evaluator output.

The tiny `scripts/smoke_offline.py` command only imports a snapshot into the
same isolated Qdrant process after deleting its collection. It is a dependency
and wiring smoke test, not clean-instance recovery or HNSW performance
evidence.

Large corpora, model files, Qdrant collections/snapshots, generated indexes,
runs and resource receipts belong outside Git.
