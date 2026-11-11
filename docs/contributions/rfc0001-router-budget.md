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
