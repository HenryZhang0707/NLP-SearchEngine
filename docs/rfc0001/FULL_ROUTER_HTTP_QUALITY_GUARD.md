# Full-corpus quality guard HTTP audit

The loopback HTTP benchmark evaluates a separate CE-33/CE-40 policy. It selects reranking depth using BM25 statistics before CE scoring. Queries above the reported threshold `0.153` use CE-40 and the rest use CE-33. Explicit lexical constraints take the fixed path. This benchmark uses CUDA BGE, Qdrant `hnsw_ef=128`, and a dedicated evaluation handler. Production serving configuration and the four-arm CE holdout with a matched random control are tracked separately.

## Frozen inputs

- Corpus: official 8,096,668-document DuReader corpus.
- Expected holdout: 808 queries from the frozen local split of official-dev. The verifier binds the exact query and qrels hashes in the configuration.
- Candidate retrieval: frozen BM25 + BGE-small dense + equal-RRF.
- Gate features: `n_terms`, `vocab_coverage`, `relative_gap`, `candidate_hits`.
- Threshold: `0.153` (frozen before holdout labels were read).
- Actions: heavy `CE-40`; cheap `fixed CE-33`.

## Reported HTTP result, pending request-log readback

| quantity | quality-guard-40 | fixed CE-33 |
| --- | ---: | ---: |
| queries | 808 | 808 |
| CE-40 queries | 231 (28.59%) | 0 (declared control) |
| mean CE pairs | 35.001 | 33.000 |
| p50 wall latency (ms) | 279.44 | 259.29 |
| p95 wall latency (ms) | 2129.56 | 2109.67 |

The reported differences relative to CE-33 are `+0.00466` nDCG@10 (paired-query bootstrap 95% CI `[+0.00160, +0.00828]`) and `+0.00553` Recall@10 (95% CI `[+0.00234, +0.00926]`). The reported mean is below the absolute cap of `36.49` CE pairs and above the 33-pair comparison cost. Both reported wall-latency percentiles also exceed the fixed control. The confidence intervals use query-level resampling.

Re-verify using the original request log, frozen model bundle, query file, and qrels. Use the original log digest in the configuration, or record a fresh capture's digest before scoring. The historical log did not record an explicit fallback field; fresh captures must include it. The audit writes a separate `audit-verified.json`.

```bash
python -m evaluation.audit_quality_guard_http --folder RUN_DIR --qrels QRELS --models MODELS_JSON \
  --queries QUERIES --requests-sha256 REQUEST_LOG_SHA256
```
