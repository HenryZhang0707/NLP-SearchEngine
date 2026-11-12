# RFC-0004 delivery reuse: current evidence and remaining gates

The local `NLP-RFC4-Delivery-20261107` delivery is useful, but its entire
directory has **not** passed the delivery verifier: vector-cache files are
missing or have unexpected sizes, including zero-byte placeholders. Do not
delete or overwrite the delivered originals. Reuse individually verified
artifacts in independent directories.

## Verified identities

- The extracted native Qdrant snapshot is 18,477,460,992 bytes with SHA256
  `2df5c889386db365e4d72d089f90bfb736c8a6a24d5bede4a10f727d9f0976e0`.
- The existing Qdrant 1.16.0 musl release archive matches the delivered
  inventory SHA256 `d4752e9bf673c7f98153264cb8f416398928c7787e1269350c179fb856de3ef7`.
  The running binary reports version 1.16.0, commit
  `0d79528705f04def1b52842f747e5a00ab1e338a`.
- Snapshot restore uses a new storage directory and loopback port 6377.
  Server configuration and exact count verify against the vector manifest;
  the server reports green status and 8,096,668 indexed vectors.
- An independent streaming scan of all 8,096,668 normalized documents confirms
  file SHA256 `bc67f3ccfe7423c039f04c7a5e7f6eae07a7f722087edf0caa7cfc694a8c45e8`
  and vector-contract fingerprint
  `0ae8b1174d0ee8e55f269c3c6d8e5f12d7e9c79fcb72c3966e99de18c9061019`.
  These hashes use different serialization and must not be compared directly.
- Exhaustive server scroll now verifies all 8,096,668 point IDs and external
  document IDs against the ordered corpus. Every point is covered, not only
  the first/last samples. A separate numeric audit reencodes one fixed document
  from each of 81 shards using the pinned model: maximum absolute vector error
  `2.1606683731079102e-07`, minimum cosine `0.9999998807907104`. This is a
  sampled numerical check and does not claim all vectors were reencoded.
- The delivered train and dev query bytes match the frozen 100k recipe:
  172 train queries (`0fc31d70fb1c4548483b3c3d115d0e89fe957c813312d6823ff155b4ef488290`)
  and 1,020 dev queries (`ce010e6ea3a498a696d653b58bcf7976b5101d4034341983429df0a44837cfd9`).

## Historical results are a separate evaluation

The delivered `root-verified-results.json` evaluates 808 test queries. It is
not this goal's 1,020-query internal-dev comparison. Its own
`latency_comparability` is `per_arm_only_mixed_epochs`; cross-arm speedup claims
must not be inferred from those timings. The test set must not become a tuning
set. Existing aggregate historical results are external reference evidence,
not substitutes for learned Router, CE-0/28/40, or equal-budget controls.

## Search parameter defect and rerun

The router worktree's original `QdrantVectorIndex` ignored attributes assigned
as `vec.search_params`, although multiple 100k harnesses assigned ef=128 that
way. Its `query_points` calls did not forward the parameters. Earlier reported
100k scores must be treated as results under the backend's effective default
configuration, not proof that ef=128 was applied.

The adapter now validates and forwards explicit ANN/exact parameters for both
supported SDK query paths. Regression tests cover forwarding, default behavior,
invalid values, and legacy search. `evaluation.capture_full_dense` additionally
passes ef=128/exact=false directly and records the actual request parameters.
Full-corpus train/dev captures are complete (172/1,020 rows, Top-200; one dev
query with explicit lexical constraints skips dense). Corrected 100k captures
are rerun separately before updating any controlled quality comparison.
Component timings recorded during concurrent audit jobs are not HTTP latency
evidence and must not be added to offline BM25 batch time.

## Outstanding acceptance work

Completed full BM25 train/dev capture, fused candidate construction, CE and learned policy
experiments, grouped nested/OOF supervision evidence, same-budget controls,
isolated end-to-end HTTP measurements, paired confidence intervals, final
regression tests, independent audit, and PR/Issue delivery remain required.
The goal stays active and the server stays on until every acceptance gate and
remote delivery check succeeds. Local aggregate reports belong in the PR;
corpus, index snapshots and large per-query records do not.

Reproduce the corpus identity check with:

```bash
python -m evaluation.audit_delivery_corpus \
  --corpus /path/to/full-corpus.jsonl \
  --manifest-path /path/to/vector_manifest.json \
  --output /path/to/new-corpus-audit.json
```

Read back every server ID and external document ID with:

```bash
python -m evaluation.audit_delivery_qdrant \
  --corpus /path/to/full-corpus.jsonl \
  --manifest-path /path/to/vector_manifest.json \
  --url http://127.0.0.1:6377 \
  --output /path/to/new-server-audit.json
```

The second audit verifies all ordered IDs/payloads and the corpus fingerprint;
it explicitly does not claim to validate every vector's numeric value.
