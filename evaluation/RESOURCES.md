# 外部资源与实验边界

本仓库不提交课程数据、模型权重或 Qdrant 产物。开题报告中的 100001 篇文档、2000 条查询和 9839 条 qrels 只能作为团队计划，不能替代公开资源的实际清单。拿到任一版本后，先用下面的通用命令校验数量、ID、qrels 引用和固定划分：

```bash
python3 -m evaluation.prepare_dataset \
  --corpus data/raw/corpus.jsonl \
  --queries data/raw/queries.jsonl \
  --qrels data/raw/qrels.tsv \
  --out data/duretrieval \
  --expected-docs 100001 \
  --expected-queries 2000 \
  --expected-qrels 9839
```

官方公开的 DuReader-Retrieval 版本不是同一份课程快照。官方说明给出的规模约为 86K 条训练查询、2K 条开发查询、4K 条测试查询和 809 万段落，并要求在百度 AI Studio 竞赛页注册后下载：

- [DuReader-Retrieval 官方说明](https://github.com/baidu/DuReader/tree/master/DuReader-Retrieval)
- [RocketQA 官方基线](https://github.com/PaddlePaddle/RocketQA/tree/main/research/DuReader-Retrieval-Baseline)
- [基线下载脚本](https://raw.githubusercontent.com/PaddlePaddle/RocketQA/main/research/DuReader-Retrieval-Baseline/script/download.sh)

本次已从 RocketQA 官方下载脚本指向的公开 CDN 获取 baseline archive，并记录下载包 SHA256：
`b051d3d966f7b0288aed95a923fc3b706176767d8bdcff8aa8fe5fff84b20662`。
该包实际包含 8,096,668 个 passage、2,000 个 dev 查询和 9,839 条去重后的 dev qrels（原始答案段落链接为 9,863 条）；四个 passage 分片各有 2,024,167 行。它没有官方 test 查询和 test qrels，因此这些 dev 数据只能标记为 `dev`，不能改名为 `test`，也不能用来声称完成课程目标版本。

官方 baseline 格式可用适配器转换，适配器会校验四个分片、连续行号映射、查询 ID、qrels 引用并流式写出 corpus。
查询输出为 `queries.dev.jsonl`，通过 JSON 转义保留原文中的换行、制表符等字符，避免产生额外查询。
提供 `--archive-sha256` 时必须同时提供 `--archive`；缺少文件或哈希不匹配时不会创建输出目录：

```bash
python -m evaluation.prepare_dureader \
  --root /path/to/dureader-retrieval-baseline-dataset \
  --out /path/to/prepared/duretrieval-official-dev \
  --archive /path/to/dureader-retrieval-baseline-dataset.tar.gz \
  --archive-sha256 b051d3d966f7b0288aed95a923fc3b706176767d8bdcff8aa8fe5fff84b20662 \
  --expected-queries 2000 --expected-qrels 9839
```

为先验证完整评测闭环，已从官方 dev corpus 固定取前 20,000 篇文档，并只保留其中有 qrel 命中的 19 条查询／19 条 qrel。该前缀索引不代表全量结果：BM25 的 nDCG@10、Recall@10、MRR@10、MAP@100 分别为 0.5032、0.5789、0.4781、0.4920，平均延迟 57.85 ms、p50 52.59 ms、p95 79.15 ms。无向量后端时 adaptive 的 10/19 次查询明确记录为 fallback；这组结果仅是官方数据前缀上的工程冒烟。

在集群上又完成了同一 prefix20k 的真实 embedding 四模式冒烟。模型固定为 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，revision `ef8c211226b894fcb81acc59f3b34ba3efd5f42`，384 维、`max_tokens=256`、cosine、CUDA 编码；19/19 条查询的 dense 和 hybrid 向量请求成功，adaptive 触发向量 10/10 次且没有 fallback。结果如下（均为该 19 条查询的 prefix smoke；延迟由集群脚本直接调用检索引擎测量，不与上一段 HTTP 延迟直接比较）：

| 模式 | nDCG@10 | Recall@10 | MRR@10 | MAP@100 | 平均延迟 ms | p50 ms | p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| lexical | 0.503242 | 0.578947 | 0.478070 | 0.491982 | 29.502598 | 26.190424 | 48.132269 |
| dense | 0.758460 | 0.526316 | 0.329887 | 0.335469 | 290.049944 | 297.190129 | 392.328207 |
| hybrid | 0.518339 | 0.789474 | 0.433396 | 0.435729 | 289.817258 | 301.648263 | 394.925098 |
| adaptive | 0.484645 | 0.578947 | 0.453947 | 0.465855 | 179.945337 | 203.351781 | 308.034930 |

对 nDCG@10 做 20,000 次逐查询 paired bootstrap，差值和 95% 区间为 lexical−dense `-0.127396 [-0.376659, 0.111861]`、lexical−hybrid `0.015096 [-0.139046, 0.159925]`、lexical−adaptive `-0.018597 [-0.119791, 0.073711]`、hybrid−adaptive `-0.033694 [-0.144463, 0.083967]`。向量库使用 `qdrant-client==1.16.0` 的 local embedded collection；这验证了真实 SDK 的持久化向量检索，但没有声称已验收 HTTP Qdrant 服务、HNSW 性能或 8M 全量语义实验。运行目录、模型和向量产物均保留在集群，不提交到仓库。

为了验证工程闭环，公开的 [C-MTEB 快照镜像](https://github.com/Lsogod/everything-ent-hybrid-rag/tree/main/data/knowledge/datasets) 可在当前网络中取得，但它只有 5000 篇文档、100 条查询和 425 条 qrels。它适合冒烟评测，不能替代课程全量数据；本地实验结果也不能写成提案的最终成绩。

RocketQA 脚本中的数据和 ERNIE 权重来自 `dataset-bj.cdn.bcebos.com`。数据包已完成哈希和结构核验；上述 prefix20k 的 GPU 编码和四模式结果已经在集群完成，但 8M 规模索引、课程目标快照、HTTP Qdrant/HNSW 以及 test labels 仍未验收。按 [评测说明](README.md) 构建 BM25、向量索引和四组实验时，模型 revision、维度、前缀、截断规则和 Qdrant 后端参数必须随实验结果一并记录。

Qdrant 适配器和快照发布逻辑已在 `offline/vector_build.py`、`online/vector_index.py` 中实现。没有可用 Qdrant 实例时，内存向量替身只能用于契约测试，不能作为 HNSW 性能结论。
