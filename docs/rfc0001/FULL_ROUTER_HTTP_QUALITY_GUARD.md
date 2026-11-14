# Full-corpus quality guard HTTP audit

This run validates the deployable cheap-feature policy on the independent 808-query full-corpus holdout. The decision is made from BM25 statistics already available after lexical retrieval; it does not inspect CE scores or qrels. Queries above the frozen gate threshold `0.15146` use CE-40, and the remaining queries use fixed CE-33. Explicit lexical constraints remain on the safe fixed path.

## Frozen inputs

- Corpus: official 8,096,668-document DuReader corpus.
- Holdout: 808 queries, disjoint from Router fitting.
- Candidate retrieval: frozen BM25 + BGE-small dense + equal-RRF.
- Gate features: `n_terms`, `vocab_coverage`, `relative_gap`, `candidate_hits`.
- Threshold: `0.15146`, frozen before holdout labels were read.
- Actions: heavy `CE-40`; cheap `fixed CE-33`.

## Independent HTTP result

| quantity | quality-guard-40 | fixed CE-33 |
| --- | ---: | ---: |
| queries | 808 | 808 |
| heavy queries | 322 (39.85% of all queries; 40.00% of eligible queries) | 808 |
| mean CE pairs | 35.790 | 33.000 |
| p50 wall latency (ms) | 316.10 | 259.29 |
| p95 wall latency (ms) | 2149.09 | 2109.67 |

Compared with fixed CE-33, the Router improves nDCG@10 by `+0.00498` with grouped bootstrap 95% CI `[+0.00149, +0.00897]`, and Recall@10 by `+0.00622` with CI `[+0.00271, +0.01022]`. The mean cost is below the `36.49` CE-pair gate.

The 91 queries newly promoted by the 40% threshold were executed over real HTTP and merged with the previously audited 717 unchanged decisions. The request log remains outside Git. The repository stores only this protocol summary, the machine-readable configuration, and the audit script.
