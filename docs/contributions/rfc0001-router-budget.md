# RFC-0001 等向量调用次数的 Router 对照

用已有的强制 lexical、强制 hybrid 和 rules-v1 adaptive 的 TREC 排名做离线质量对照。逐查询验证 adaptive 排名与它实际执行的分支一致，并检查 HTTP 覆盖、路由原因和回退记录。显式词法约束查询固定走 lexical，仍计入指标分母。 [RFC-0001](https://github.com/ShwStone/NLP-SearchEngine/issues/3)

对照从 N 条可走向量分支的查询中均匀无放回选恰好 B 条走 hybrid，其余走 lexical。B 等于规则 Router 的实际向量调用次数。每条可选查询的边际入选概率是 B/N，因此可直接计算随机对照的期望分数。置信区间用 10,000 次查询级配对 percentile bootstrap，seed 为 2026，重采样时固定原 B/N。区间衡量规则 Router 与随机对照期望之差的查询抽样不确定性。

| 评测集 | 查询数 | 可调用向量 | 两臂的向量调用预算 | 规则 nDCG@10 | 随机对照期望 nDCG@10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| RocketQA 官方 8M 语料，official-dev 内的本地留出 | 808 | 806 | 595 | 0.18764 | 0.18775 |
| 自爬新闻 20k 开发集，现有辅助标注 | 50 | 50 | 9 | 0.89374 | 0.89498 |

| 指标 | 8M 本地留出，规则减随机期望及 95% CI | 新闻 20k dev，规则减随机期望及 95% CI |
| --- | --- | --- |
| nDCG@10 | −0.00012 [−0.00592, +0.00543] | −0.00124 [−0.02984, +0.02268] |
| Recall@10 | −0.00282 [−0.00977, +0.00374] | −0.01429 [−0.05911, +0.02145] |
| MRR@10 | +0.00280 [−0.00694, +0.01207] | +0.00680 [−0.01560, +0.02580] |
| MAP@100 | +0.00034 [−0.00445, +0.00494] | +0.00689 [−0.01548, +0.02920] |

两组数据的四项区间均跨过 0。8M 的约束感知固定 hybrid nDCG@10 为 0.20949，需要 806 次向量调用。回顾性 808 条查询来自 official-dev，新闻开发集沿用已有辅助标注。向量调用次数只反映分流预算，耗时和学习型策略需各自验证。

```bash
python3 -m evaluation.router_budget \
  --queries queries.jsonl --qrels qrels.tsv \
  --lexical-run lexical.trec --hybrid-run hybrid.trec \
  --adaptive-run adaptive.trec --adaptive-receipt adaptive.requests.json \
  --lexical-only lexical-only-qids.json \
  --k 10 --map-k 100 --samples 10000 --seed 2026 \
  --output-json budget-comparison.json
```

`lexical-only-qids.json` 是预先冻结的查询 ID 数组。把 adaptive 的请求 journal 放在收据旁边。本次 8M 与新闻 20k 结果 SHA-256 分别为 `3f5e34c7ce9af53d96056425bd7d925f3fbefbd789f52522e3a4d996eb4c0f86`、`425120e87b8e4566f83445e67cc545de6f6fb174431d835583d58003807fd583`。运行、标注和结果收据保存在 Git 外。

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

特征导出回执绑定原始查询、词法 TREC、特征文件和实际语料指纹。训练入口验证回执，再将语料、候选池、RRF 和特征版本绑定到模型。在线引擎启动时校验实际语料和配置。回放同时要求冻结的实验身份及三臂 TREC、指标和请求日志的摘要。历史 JSONL 请求证据缺少逐请求配置，该边界随结果记录。

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
