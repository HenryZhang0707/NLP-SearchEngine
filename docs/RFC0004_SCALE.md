# RFC-0004 scale path

This document describes the optional scale support currently present in the
repository. The native full-corpus lexical merge and public compact-reader
acceptance passed on 2026-11-06 for 8,096,668 documents. This is an engineering
handoff. The previous four-mode quality attempt failed without sealed arms;
no accepted full-scale retrieval-quality result exists yet.

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

### Process-termination recovery correction

`offline.checkpoint_build` binds full input SHA256/size, dataset,
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
to the original stream builder. These checks establish process-crash recovery;
Windows power-loss and disk-failure tolerance are not claimed. Resume with the
original runtime and source revision. A reviewed bootstrap
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

## Native full-corpus merge and reader acceptance (2026-11-06)

The native supervisor, merge and reader receipts all report `PASS`, with worker
exit code 0. The merged lexical/compact bundle contains 8,096,668 documents and
8,578,589 terms. Its ordered corpus fingerprint is
`0ae8b1174d0ee8e55f269c3c6d8e5f12d7e9c79fcb72c3966e99de18c9061019`.
The frozen native program rehashed inputs and outputs, completed full semantic
index checks and the ordered fingerprint check, and probed internal IDs 0,
4,048,334 and 8,096,667 through the public `DiskIndex` reader.

The root audit verified the exact receipt chain and all recorded bindings;
it did not independently download and rehash the roughly 107 GB of inputs.
Supervisor/merge/reader receipt SHA256 values are respectively:

- `bc9b425ccc15f1f87576e03c37d13b7ee6bae24b6445a3aebf6895070d5e2b59`
- `1d4704da31373db5bbb6b441a5dc23f84fe1ea3792d95057c5528ec6bb95f0f3`
- `aa008c41a3bf323e596147893d19b174db2c1b582207ed7609a8ff89756c01ad`

The request was 2 CPUs / 10 GiB / 0 GPUs. Supervisor wall time was 40,563.54
seconds, worker peak RSS 392,413,184 bytes, and exact-membership cgroup peak
2,201,608,192 bytes; the cgroup was not process-exclusive. The final receipt
records no OOM or failcnt increase. These measurements admit this native merge
and reader run. Earlier failed attempts and bounded pilots remain separate
evidence. The previous 808-query four-mode attempt failed on transport errors
without sealed arms. This merge acceptance establishes no retrieval-quality result.

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
This is a real numerical admission, not retrieval-quality evidence.

An independent local-cache 2080Ti gate now reports `GPU_PILOT_PASS` using
1 GPU / 4 CPUs / 4 GiB, with largest CPU/GPU absolute difference
`1.6391277313232422e-7 < 1e-5`. The full-text batch16/32/64 throughput and
process RSS measurements are in `evaluation/RESOURCES.md`; the flat cgroup path
has no admitted exact-membership mapping and is not an authoritative job peak.
Historical GPU tasks and failures remain protected. This bounded numerical and
throughput gate does not admit an 8M build or retrieval quality.

`scripts/build_vectors.py` also accepts `--corpus` with mandatory
`--corpus-sha256` and `--corpus-n-docs`, mutually exclusive with `--index`.
`CorpusDocuments` scans and verifies immutable raw JSONL before model loading,
keeps two uint64 offset/length arrays, and preserves physical row IDs and all
original fields. It rejects blank lines and observable input changes. The
existing vector builder still checks external-ID uniqueness and computes the
unchanged ordered fingerprint before publication. It creates no lexical files
and does not relax final dual-index fingerprint matching.

This document source has now passed a real full-input admission, independently
of the lexical build: it scanned and parsed all 8,096,668 rows of the
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
the independent real-service graph/restore admission below also verifies it
on a bounded 2,048-document pilot.

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
that historical attempt did not reach encoding, graph search or clean restore.
The supervisor cleaned only its new process group within1193.16s. Its failure
receipt remains preserved; subsequent success does not establish its root cause.

An independent local-cache BGE/512/FP32 and Qdrant1.16 pilot now reports
`REAL_BGE_HNSW_PILOT_PASS`, using 2 CPUs / 4 GiB / 0 GPUs. A before snapshot and
B after fresh-process/empty-storage restore both have exact count=2,048,
all nonempty segments indexed, and two stable readiness observations. In each
16-query ANN window, nonempty segment HNSW counters increase by 16 with
plain/exact increments of 0; empty-segment plain increments of 16 are recorded separately.
These counters measure segment searches. ANN uses `hnsw_ef=256`, `exact=false`;
ANN and exact results each retain their IDs, order and scores across restore
within absolute tolerance `1e-5`. This is a mechanical restore check, not a
quality or recall measurement. The 10,060,800-byte snapshot SHA256 is
`69f84ccf6607cd57a280010af9a2ec900f52b676b6e911e60e0773fed9394d39`;
the published artifact is in static ROOT and does not rely on a `/tmp` pointer.
Worker/supervisor times are 656.764/667.318 seconds, `COMPLETE`, exit 0, no timeout.
Constructor time 314.718 seconds does not localize the historical wait; the
90.98-second document encoding is CPU engineering throughput for this pilot.
Receipt hashes and full limits are in `evaluation/RESOURCES.md`; qrels and
quality evaluation were not used, and 8M/four-mode/paired-bootstrap gates remain.

A separate 10k full-builder probe initially stopped before its worker because
cgroup accounting paths were unresolved; its 43.44s is not build-throughput
evidence. The native repair passed using 2 CPUs / 2 GiB, with core build 31.215s
(320.359 docs/s), exact-process-membership cgroup peak 858,669,056 bytes,
no OOM and two full reader reopens. This cgroup is not process-exclusive.
The 100k successor's first attempt failed before build because the wrapper
incorrectly pinned Unicode14 for Python3.10. The repair strictly pins Unicode13
per the [official Python3.10 documentation](https://docs.python.org/3.10/library/unicodedata.html);
the repaired run completed A1 build and independent acceptance, but compact hit
the unchanged 90% guard on a 4 GiB cgroup. Process RSS was about 46 MB while
private WAL reached 1.98 GB; no OOM occurred. A live same-connection SELECT
pinned WAL frames. Bounded keyset pages close that reader before writes/commits
in both postings and terms export. The new regression fails on the old code
and passes with the fix; all eight public tiny artifacts are byte-identical,
including recovery at the tested fsync/commit page boundary. WAL/FULL, save
ordering, format and validation remain unchanged. The native 100k rerun under the same 4 GiB budget passed A1 and compact,
including two packed-reader reopens and SHA256 readback of 13 static files.
That 100k result did not establish 8M acceptance; the later native full-corpus
merge and reader acceptance is recorded above.

A 5,000-document native split-mount probe passed with unchanged A1 code:
sequential artifacts were written to LeoFS while the WAL/FULL checkpoint stayed
on private overlay storage. Hard exits after ingest fsync, during postings
export and after publication rename all resumed; deliberately overlong tails
were truncated and all five final file hashes matched the private-storage
oracle. The probe requested 2 CPUs / 2 GiB and took 50.312 seconds including both builds,
faults and verification. Its maximum child RSS was 24,588,288 bytes; exact
cgroup peak memory was unavailable and is not inferred from RSS. Receipt SHA256:
`2ec61371f03ea87f834238690e520816100e910e4de37a14877a0968b0da8f84`.
This verifies filesystem semantics, not full-shard throughput or safe concurrent
capacity. Existing jobs were kept intact. A subsequent 100,000-document native
split-mount A1 build also matched all five private-baseline artifact hashes.
It requested 2 CPUs / 4 GiB, took 631.627 seconds for A1, and recorded an exact
cgroup peak of 1,387,294,720 bytes with no OOM or failcnt increase. Sampled
private/shared owned peaks were 779,751,424 / 811,275,616 bytes. The earlier
private A1 build took 602.202 seconds; the 1.049 ratio is descriptive because
load and hardware placement were not randomized. Receipt SHA256:
`1bf2c4fd513e33b297eab85078070acf190d452f4840b51fc241c9c8543bb76e`.
This second probe covers A1 only; it does not establish a 506k or 8M peak,
physical backing identity, or safe multi-worker capacity. Further scheduling
requires fresh resource checks and separate admission of the proposed workload.

The historical NFS check does not describe every private mount: static ROOT
was subsequently observed as LeoFS. Qdrant runtime data uses isolated storage.
Container `/tmp` reports overlay with physical backing UNKNOWN; that alone
does not prove physical SSD, durable storage or admit an 8M build.
YAML configuration discovery and non-default port/config checks are mandatory;
a silently ignored JSON config is a failed setup, not an accepted downgrade.

## Remaining production gate

Source `382a8cc` passes core 69/69, evaluation 83/83 and native CPython
3.10.14 / Unicode13 unittest discovery 153/153, compilation and diff checks.
The same native cluster dependency environment separately passes core 69,
evaluation 83 and scorer-equivalence 7 checks. Unchanged frontend code retains
its earlier Node 7/7 result. These checks do not
replace full-index acceptance or full-scale retrieval-quality experiments;
real-model CPU/GPU and bounded HNSW/restore gates now have independent receipts.

The legacy `offline.build` path still materializes the conformance oracle in
memory; use `offline.stream_build` when measuring a large lexical build. The
optional compact conversion happens after a source build and therefore does
not remove that source peak. `offline.vector_build.QdrantSnapshotWriter` now
uses a temporary SQLite identity table. The completed 8M vector cache and
two clean Qdrant restores have separate resource receipts in
`evaluation/RESOURCES.md`; native lexical merge and reader acceptance are
recorded above. Equivalent runtime fast paths and native CPU checks have passed;
the fixed dev32-to-formal808 protocol is frozen, with no accepted full-scale
quality result yet. A Qdrant HNSW result
must additionally include server version,
collection metadata, exact point count, snapshot hash, clean-instance restore,
peak RSS/disk, throughput and four-mode evaluator output.

The tiny `scripts/smoke_offline.py` command only imports a snapshot into the
same isolated Qdrant process after deleting its collection. It is a dependency
and wiring smoke test, not clean-instance recovery or HNSW performance
evidence.

Large corpora, model files, Qdrant collections/snapshots, generated indexes,
runs and resource receipts belong outside Git.

For parallel offline builds, freeze the raw corpus SHA256 and contiguous shard
ranges before building. For each shard, independently hash its canonical A1
documents with shard-local IDs. Build A1 and compact for each shard, then pass
the ordered pairs to `python scripts/merge_indexes.py --plan PLAN.json --shard
A1_0 COMPACT_0 --shard A1_1 COMPACT_1 --out BUNDLE --state-dir STATE`.
The plan fields are `dataset`, `source_sha256`, `n_docs`, and `shards`.
Each shard entry contains `start`, `stop`, `expected_docs_sha256`, and an
optional `raw_sha256`. The output contains `lexical/` and `compact/`.
Keep the input shards fixed while building or resuming.
Keep the checkpoint directory on a local filesystem. Both the checkpoint
database and the active public terms export are written there. The output
staging directory receives the closed SQLite file and verifies its SHA256.
On a separate output volume, verify directory `fsync`, truncation, and
exclusive atomic rename before allocating a full build.

Validate the merged compact index against the separately published Qdrant
artifacts in place:

```sh
python -m offline.validate --lexical-index BUNDLE/compact --vector-artifacts QDRANT_DIR
```

This checks the lexical files, document count, ordered corpus fingerprint and
snapshot SHA256. Validate the restored collection and four search modes against
a running service separately.

`python scripts/cache_vectors.py --help` exposes separate producer and consumer
commands. Producers use distinct shard IDs and the same corpus hash, model
specification, batch size, and cache directory. The cache directory requires
exclusive creation and hard links. The consumer validates each sealed chunk
and still uses the public Qdrant snapshot writer. Completed chunks can be
reused after interruption. Inspect any incomplete claims before recovery.
