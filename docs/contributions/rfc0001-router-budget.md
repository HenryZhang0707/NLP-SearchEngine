# RFC-0001 等向量调用次数的 Router 对照

用已有的强制 lexical、强制 hybrid 和 rules-v1 adaptive 的 TREC 排名做离线质量对照。逐查询验证 adaptive 排名与它实际执行的分支一致，并检查 HTTP 覆盖、路由原因和回退记录。显式词法约束查询固定走 lexical，仍计入指标分母。 [RFC-0001](https://github.com/ShwStone/NLP-SearchEngine/issues/3)

对照从 N 条可走向量分支的查询中均匀无放回选恰好 B 条走 hybrid，其余走 lexical。B 等于规则 Router 的实际向量调用次数。每条可选查询的边际入选概率是 B/N，因此可直接计算随机对照的期望分数。置信区间用 10,000 次查询级配对 percentile bootstrap，seed 为 2026，重采样时固定原 B/N。区间衡量规则 Router 与随机对照期望之差的查询抽样不确定性。

| 评测集 | 查询数 | 可调用向量 | 两臂的向量调用预算 | 规则 nDCG@10 | 随机对照期望 nDCG@10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| RocketQA 官方 8M 语料，official-dev 内的本地留出 | 808 | 806 | 595 | 0.18764 | 0.18775 |
| 自爬新闻 20k 开发集，现有辅助标注 | 50 | 50 | 9 | 0.89374 | 0.89498 |
| 协作版 DuReader 100k 开发集 | 1,020 | 1,019 | 748 | 0.20495 | 0.20299 |

| 指标 | 8M 本地留出，规则减随机期望及 95% CI | 新闻 20k dev，规则减随机期望及 95% CI | DuReader 100k dev，规则减随机期望及 95% CI |
| --- | --- | --- | --- |
| nDCG@10 | −0.00012 [−0.00592, +0.00543] | −0.00124 [−0.02984, +0.02268] | +0.00197 [−0.00287, +0.00669] |
| Recall@10 | −0.00282 [−0.00977, +0.00374] | −0.01429 [−0.05911, +0.02145] | +0.00489 [−0.00085, +0.01046] |
| MRR@10 | +0.00280 [−0.00694, +0.01207] | +0.00680 [−0.01560, +0.02580] | +0.00415 [−0.00433, +0.01255] |
| MAP@100 | +0.00034 [−0.00445, +0.00494] | +0.00689 [−0.01548, +0.02920] | −0.00097 [−0.00487, +0.00291] |

三组数据的四项区间均跨过 0。8M 的约束感知固定 hybrid nDCG@10 为 0.20949，需要 806 次向量调用；100k 为 0.22799，需要 1,019 次向量调用。这两个离线对照都将显式约束查询固定给 BM25，因此与可执行带过滤向量分支的强制 Hybrid HTTP 模式略有差异。回顾性 808 条查询来自 official-dev，新闻开发集沿用已有辅助标注。向量调用次数只反映分流预算，耗时和学习型策略需各自验证。

```bash
python3 -m evaluation.router_budget \
  --queries queries.jsonl --qrels qrels.tsv \
  --lexical-run lexical.trec --hybrid-run hybrid.trec \
  --adaptive-run adaptive.trec --adaptive-receipt adaptive.requests.json \
  --lexical-only lexical-only-qids.json \
  --k 10 --map-k 100 --samples 10000 --seed 2026 \
  --output-json budget-comparison.json
```

`lexical-only-qids.json` 是预先冻结的查询 ID 数组。把 adaptive 的请求 journal 放在收据旁边。本次 8M、新闻 20k 和 DuReader 100k 结果 SHA-256 分别为 `3f5e34c7ce9af53d96056425bd7d925f3fbefbd789f52522e3a4d996eb4c0f86`、`425120e87b8e4566f83445e67cc545de6f6fb174431d835583d58003807fd583`、`774837b0e14c696c700c3ab586d3e9464b4a57ecbbc509fd124a21f6f6d924e8`。运行、标注和结果收据保存在 Git 外。

## BM25 优先的线性收益预测

候选 B 使用线上第一阶段已经得到的五项特征，包括词项数与命中数的 `log1p`、词表覆盖率、首两名相对分差及分差缺失标记。监督目标为逐查询 hybrid 与 lexical 的 nDCG@10 差。仅在训练查询上做标准化、拟合 `alpha=1` 的岭回归并校准向量触发阈值，训练调用数与规则相同。显式词法约束沿用引擎的词法路径。

在同一新闻 20k 语料、BGE/Qdrant 快照、候选池 200、RRF 60 和 `hnsw_ef=256` 下，用已有辅助标注的 50 条 dev 查询训练和校准，在原 150 条 test 查询上回顾性验证。两个 split 的查询 ID 和归一化查询文本均无重叠。提取特征时，200 条查询的前 100 个词法文档与原始 TREC 顺序一致。训练阶段规则与学习阈值各调用向量 9 次。

150 条查询的预测先封存。等预算回放按预测增益降序、查询 ID 升序，选与原规则相同的 32 条走 hybrid。训练阈值在该批查询上触发 31 次。等预算质量差来自 10,000 次配对 bootstrap，seed 为 2026。

| 指标 | 规则 | 学习型等预算回放 | 学习型减规则及 95% CI |
| --- | ---: | ---: | --- |
| nDCG@10 | 0.90825 | 0.90434 | −0.00391 [−0.02599, +0.01594] |
| Recall@10 | 0.87314 | 0.87950 | +0.00636 [−0.01613, +0.02907] |
| MRR@10 | 0.97556 | 0.95578 | −0.01978 [−0.04512, +0.00300] |
| MAP@100 | 0.85141 | 0.84540 | −0.00601 [−0.02916, +0.01417] |

四项区间均跨过 0。固定 lexical 的 nDCG@10 为 0.91676。等预算回放需要预先知道整批查询和预算，在线单请求使用冻结训练阈值。该历史辅助标注试验尚不能支持上线收益，独立人工标注仍待完成。100k 的 CE 动作、学习排序器和成本分配沿用 [PR #9](https://github.com/ShwStone/NLP-SearchEngine/pull/9) 的协作主线。

在恢复的同一 20k Qdrant 快照上，通过真实 `POST /search` 复验了冻结阈值。150 次请求全部返回 HTTP 200，31 次向量调用均成功且无回退。逐查询的决策与文档排序均和回放一致。在线验收收据摘要为 `3db6d6ec5e9df809a7132b4c0e285c8260d307a2a0b33c4d65ef7c9ef327726e`。

特征导出回执绑定原始查询、词法 TREC、特征文件和实际语料指纹。训练入口验证回执，再将语料、候选池、RRF 和特征版本绑定到模型。在线引擎启动时校验这三项运行条件；编码器、向量快照和 `hnsw_ef` 的身份由实验验收另行核验。回放同时要求冻结的实验身份及三臂 TREC、指标和请求日志的摘要。历史 JSONL 请求证据缺少逐请求配置，该边界随结果记录。换编码器、快照或 ANN 参数后，须重新验证策略效果。

```bash
python3 scripts/export_router_features.py --index compact --queries train.tsv \
  --lexical-run train.lexical.trec --expected-corpus-sha256 "$CORPUS_SHA" \
  --output train.features.jsonl > train.features-receipt.json
python3 -m evaluation.train_router_gain --train-features train.features.jsonl \
  --feature-receipt train.features-receipt.json \
  --train-queries train.tsv --train-qrels train.qrels.tsv \
  --lexical-run train.lexical.trec --hybrid-run train.hybrid.trec \
  --adaptive-receipt train.adaptive.trec.requests.json --output-model gain.json
```

用 `scripts/replay_router_gain.py freeze` 的 `--experiment-binding` 和 `--feature-receipt` 生成留出决策，再用 `score` 加载留出标注、三臂排序及 `--lexical-requests/--hybrid-requests/--adaptive-requests` 请求证据。在线试验在本地配置 `NLP_ROUTER_GAIN_MODEL`、`NLP_ROUTER_GAIN_SHA256`，显式选择工厂 `online.linear_gain_router:load_gain_router`。模型摘要 `46688933af468104b9b79862b97f752a535c9b895f36c05605d65f8595d14eb9`，回放结果摘要 `e953eaf4abf5f8cf38657cf70d3f9363bf084d10c645964de6ae50545ec70a8a`。训练模型、特征、查询和运行记录保存在 Git 外。

### DuReader 100k 开发集复验

在相同 BGE/Qdrant 快照、候选池和 RRF 参数下，训练划分 172 条查询的 lexical、hybrid、adaptive 三臂共 516 次真实 HTTP 请求。训练和开发的查询 ID 及归一化文本均无重叠。172 条训练查询的模型和阈值仅用训练数据确定，规则和学习阈值在训练集均触发 118 次向量检索。训练和开发的词法特征分别核对了 172 和 1,020 条原始排序。

开发集以规则 Router 观察到的 748 次为共同预算。显式约束查询固定走词法，仍纳入 1,020 条查询分母。按预测值冻结整批决策后，用已验收的固定分支排序和开发 qrels 回放评分。质量区间为 10,000 次配对 bootstrap，seed 2026。

| 指标 | 规则 | 线性收益等预算回放 | 线性收益减规则及 95% CI |
| --- | ---: | ---: | --- |
| nDCG@10 | 0.20495 | 0.20364 | −0.00132 [−0.00505, +0.00257] |
| Recall@10 | 0.24104 | 0.23887 | −0.00217 [−0.00729, +0.00283] |
| MRR@10 | 0.31704 | 0.31641 | −0.00063 [−0.00681, +0.00578] |
| MAP@100 | 0.18658 | 0.18563 | −0.00094 [−0.00348, +0.00171] |

四项区间均跨过零。训练阈值在开发集离线回放触发 715 次，nDCG@10 为 0.20164。该结果是开发集分支回放；学习型策略没有在这 1,020 条查询上另跑 HTTP 计时，也未用于 808 条全量留出的复评。现有证据不支持更换默认策略。

冻结计划、模型及回放结果的 SHA-256 分别为
`eb8835e731fd82107284c55a582e8c846f172dfef926391b10ccc22a2e10b17b`、
`2da54fc59faa94f46d0e4f45a114c996dbbf89bacdac1efe31b01b67fdf4ad57` 和
`1faa989a1f5d5dc93e6d9f5221d86c7379b5c452e13c441b294dbe8209b81f9e`。
源排序、特征、模型、决策及逐阶段验收保存在 Git 外。
