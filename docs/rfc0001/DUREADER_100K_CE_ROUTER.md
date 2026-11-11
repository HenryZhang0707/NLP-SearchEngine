# DuReader 100k CE and learned Router experiment

This stage uses only the frozen 100k train split for fitting and the frozen internal-dev split for the final report. Candidate capture is Top-200 lexical/dense/hybrid with the same BGE-small/Qdrant manifest as the baseline. The BGE reranker is the pinned local `bge-reranker-base` inventory; no dev labels are used to fit the ranker or Router.

The raw CE action reranks the first 28 or 40 hybrid candidates and leaves the tail in first-stage order. `CE-0` is equal-RRF with no reranking. The learned ranker is a train-only ridge model over CE score and candidate position. The learned Router is a train-only logistic model over query-side features (term count, lexical candidate count, dense overlap, and lexical score gap); it decides whether to spend 40 CE pairs.

| action | nDCG@10 | Recall@10 | MRR | MAP@100 | CE p50/p95 (ms) |
|---|---:|---:|---:|---:|---:|
| CE-0 | 0.22787 | 0.26479 | 0.34746 | 0.20741 | 0 / 0 |
| CE-28 raw | 0.40900 | 0.42592 | 0.58177 | 0.34723 | 38.01 / 48.36 |
| CE-40 raw | 0.48548 | 0.52831 | 0.63583 | 0.41379 | 49.00 / 60.90 |
| CE-28 + learned ranker | 0.40331 | 0.42303 | 0.57604 | 0.34125 | 49.04 / 60.22 |
| CE-40 + learned ranker | 0.51276 | 0.56261 | 0.65878 | 0.42935 | 49.04 / 60.22 |
| learned Router (CE-40 action) | 0.39438 | 0.43529 | 0.53826 | 0.33819 | 60.7% heavy |

The CE-40 raw action improves nDCG by 0.25761 over equal RRF (paired 95% CI [0.24238, 0.27312]). The train-only learned ranker adds another 0.02728 nDCG (paired 95% CI [0.01780, 0.03676]). The learned Router improves nDCG by 0.16650 over equal RRF (paired 95% CI [0.15174, 0.18110]) at a 60.7% heavy rate in the final no-position-feature rerun. Its cost and quality must still be qualified through the production HTTP harness before being treated as an online acceptance result.

Run candidate capture with `evaluation/capture_dureader100k.py`, then train/evaluate with `evaluation/train_eval_ce_router_dureader100k.py`. The machine-readable result, input hashes and split protocol are in `dureader-100k-ce-router-summary.json`. Large per-query captures remain local and are intentionally excluded from the PR.
