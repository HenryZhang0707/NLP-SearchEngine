# Corrected DuReader CE/Router experiment

This follow-up repairs reproducibility gaps in the earlier 100k report.
Dense retrieval explicitly uses HNSW ef=128/exact=false. Historical lexical
captures are reused with external/internal IDs bound to the verified index;
this reuse is recorded rather than presented as a new lexical run. Equal RRF
uses c=60 and global internal docid ties. Explicit constraints remain lexical.

## Label and split discipline

`capture_ce_actions` scans the corpus with bounded memory, keeps only selected
candidate texts, verifies the source bytes/count, and scores CE-28 and CE-40
separately without accessing qrels. Large scores/captures stay local.

`train_dureader_policies` trains only on 172 internal-train queries. Normalized
duplicate query texts and shared positive-document IDs define connected
training groups (171 groups). Four outer GroupKFold splits and three inner
splits preserve group separation. Inner folds select ridge regularization
from 0.1/1/10/100; all OOF assignments and selections are recorded locally.
Dev predictions and model exports are saved before opening dev qrels. The
1,020 internal-dev queries remain historically observed development evidence,
not an untouched final test. Grouped paired bootstrap uses 1,014 dev groups.

The gate estimates CE-40's nDCG gain using only term count, corpus vocabulary
coverage, BM25 relative gap and lexical candidate count. Its threshold comes
from training OOF predictions with a predeclared 60% train heavy-budget ceiling.
The exact same functions and exported scaler/linear coefficients are used in
serving. Learned candidate ranking uses CE score and hybrid/lexical/dense rank
features; no relevance grades are features.

Random controls use label-independent seeded query hashes and exactly match
the Router's realized heavy count. A fixed CE candidate count matches average
pair budget after rounding down; its quality can reuse cached pair scores, but
its latency must be measured by separately executing that budget over HTTP.

## Observed v3 diagnostic results

| Policy | nDCG@10 | Recall@10 | Mean CE pairs |
|---|---:|---:|---:|
| equal RRF / CE-0 | 0.22767 | 0.26430 | 0 |
| old rules Router | 0.20464 | 0.24055 | 0 |
| raw CE-28 | 0.40895 | 0.42601 | 28 |
| raw CE-40 | 0.48586 | 0.52905 | 40 |
| learned Router | 0.38770 | 0.42404 | 24 |
| random seed 20261113 | 0.38441 | 0.42496 | 24 |
| random seed 20261114 | 0.38400 | 0.42346 | 24 |
| fixed CE-24 | 0.38116 | 0.38593 | 24 |
| learned CE-28 ranker | 0.38454 | 0.42047 | 28 |
| learned CE-40 ranker | 0.47839 | 0.52118 | 40 |

Router minus same-budget random nDCG differences are +0.00329 and +0.00370,
with grouped 95% CIs [-0.01232, 0.01848] and [-0.01210, 0.01975]. There is no
reliable evidence of superiority over random gating. Improvement over CE-0
alone is insufficient to establish useful routing.

The added rank features regress against raw CE. The v4 repair therefore adds
raw CE as an explicit candidate in training-only inner selection, with raw
preferred on ties. Both full-train selections reject learned reranking and
choose raw CE. This protects quality using training evidence; it is not a
claim that a learned ranker improved accuracy. The v3 negative results remain
preserved and must not be silently replaced. The gate remains experimental.

## Remaining gates

Actual HTTP pilot/all-query evaluation must confirm the exported decisions
and candidate IDs. Component times during background batch work are not final
latency evidence. Complete full-corpus BM25 capture, fusion, CE scoring, nested
training, same-budget controls, backend parity, isolated HTTP measurements,
independent audit, PR/Issue update, and remote verification remain required.
No shutdown is authorized by partial experiment success.
