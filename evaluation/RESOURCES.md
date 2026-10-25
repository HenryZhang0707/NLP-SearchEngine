# 外部资源与实验边界

本仓库不提交课程数据、模型权重或 Qdrant 产物。提案中的实验规模是课程组约定的版本：
100001 篇文档、2000 条查询和 9839 条 qrels。拿到课程文件后，先用下面的命令校验数量、ID、qrels 引用和固定划分：

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

为了验证工程闭环，公开的 [C-MTEB 快照镜像](https://github.com/Lsogod/everything-ent-hybrid-rag/tree/main/data/knowledge/datasets) 可在当前网络中取得，但它只有 5000 篇文档、100 条查询和 425 条 qrels。它适合冒烟评测，不能替代课程全量数据；本地实验结果也不能写成提案的最终成绩。

RocketQA 脚本中的数据和 ERNIE 权重来自 `dataset-bj.cdn.bcebos.com`。当前云环境访问该 CDN 和 AI Studio 均被网络代理拒绝（HTTP 403），因此不能把官方版本冒充课程版本，也不能生成未经资源核验的全量结果。获得课程资源后，按 [评测说明](README.md) 构建 BM25、向量索引和四组实验；模型 revision、维度、前缀、截断规则和 Qdrant HNSW 参数必须随实验结果一并记录。

Qdrant 适配器和快照发布逻辑已在 `offline/vector_build.py`、`online/vector_index.py` 中实现。没有可用 Qdrant 实例时，内存向量替身只能用于契约测试，不能作为 HNSW 性能结论。
