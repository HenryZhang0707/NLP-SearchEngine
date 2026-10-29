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

## 2026-10-29 集群验收记录（受限前缀烟雾实验）

以下结果来自一次固定的集群任务，所有生成数据、权重、索引和 TREC run 均留在任务私有临时目录，没有提交到仓库。公开 baseline archive 为 4,214,306,577 bytes，SHA256 为
`b051d3d966f7b0288aed95a923fc3b706176767d8bdcff8aa8fe5fff84b20662`；准备后的 dev 资源为 8,096,668 篇文档、2,000 条查询和 9,839 条 qrels。其 passage map、corpus、queries.dev 和 qrels.dev 的 SHA256 分别为 `9b091ee62278b02973c6fa6de5798c3b0bee9a493613ab29a4b807efdd387411a`、`bc67f3ccfe7423c039f04c7a5e7f6eae07a7f722087edf0caa7cfc694a8c45e8`、`3be3067e73022ea5b3b919196baae29dc2d50c6aa7053ad911e2b3d5cf8d3899a` 和 `150f4665e779c0c876a46102d9a00fa467759847bc01ff344ee6c7f92c43fd79`。

为控制内存和验证完整 HTTP 闭环，从该 dev corpus 流式取前 20,000 篇文档，固定 seed `20261028`，按 dev/test/train=0.5/0.4/0.1 得到 7/9/3 条查询。前缀 corpus SHA256 为 `7e2a20ca5f7a75ece81efffbb45f10983c90ebaf500f60f60a4a53f9c775a55bfc8`；test qrels、queries 的 SHA256 分别为 `b07146daa42cd3b53c4c55779625ecce20dca3d337d86a64eb03a1c3d288f1cc2` 和 `f05c990beb04a4997b7910149d4a128f74823aae6f1af7b09c84c80ec5f20eff5`。这仍是官方 dev 的前缀烟雾数据，不能替代课程目标快照，也不是团队自爬测试集。

语义编码使用 `BAAI/bge-small-zh-v1.5`，固定 revision `7999e1d3359715c523056ef9478215996d62a620`，512 维、CLS pooling、最大 512 tokens、L2 归一化；`model.safetensors` SHA256 为 `354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026`，`tokenizer.json` SHA256 为 `48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26`。运行时为 PyTorch 2.3.0、Transformers 4.57.1、NVIDIA A800 80GB；CUDA 编码结果有限、归一化范数为 1，重复编码最大绝对差为 0。标准 Hugging Face 端点在该任务中不可达，权重通过固定 revision 的公开镜像取得；镜像只用于下载，不改变模型 ID 或 revision。

路由参数在 dev 上先固定为 `candidate_k=200`、`rrf_c=60`、`min_vocab_coverage=0.7`、`min_relative_gap=0.1`、`policy_version=rules-v1`，随后只在 test 上报告结果。test 的四模式结果如下；dense/hybrid/adaptive 使用真实 BGE 查询编码和 20k 向量，但向量读取器是 `memory-exact` NumPy 全扫替身，因此不能解释为 Qdrant HNSW 性能：

| mode | nDCG@10 | Recall@10 | MRR@10 | MAP@100 | mean ms | p50 ms | p95 ms | vector success / fallback |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| lexical | 0.625659 | 0.666667 | 0.611111 | 0.626455 | 50.90 | 49.29 | 61.09 | 0 / 0 |
| dense | 0.958992 | 1.000000 | 0.944444 | 0.944444 | 198.48 | 202.67 | 218.39 | 9 / 0 |
| hybrid | 0.791378 | 1.000000 | 0.730688 | 0.730688 | 200.81 | 200.91 | 223.86 | 9 / 0 |
| adaptive | 0.713333 | 0.888889 | 0.659259 | 0.666667 | 133.46 | 169.74 | 229.49 | 5 / 0 |

配对 bootstrap 使用 test 的 9 条查询、10,000 次重采样和 seed `20261028`，差值定义为后者减前者。lexical→hybrid 的 nDCG/Recall/MRR/MAP 差值及 95% percentile CI 为 `+0.165719 [0.037037, 0.294400]`、`+0.333333 [0.000000, 0.666667]`、`+0.119577 [0.015873, 0.238095]`、`+0.104233 [0.011111, 0.222222]`；lexical→adaptive 对应为 `+0.087674 [0.000000, 0.207683]`、`+0.222222 [0.000000, 0.555556]`、`+0.048148 [0.000000, 0.122222]`、`+0.040212 [0.000000, 0.098413]`。样本量很小，CI 只证明这次前缀烟雾的配对差异，不能外推到全量。

真实 Qdrant server 小批验收已完成：服务端版本 `1.16.0`，客户端版本 `1.16.0`。仓库自身 `QdrantSnapshotWriter` 创建 `rfc4_writer_20261029b` collection，写入 3 个 4 维 cosine 向量和完整 `nlp_search_engine` metadata；HNSW 参数为 `m=16`、`ef_construct=100`。快照文件 112,640 bytes，SHA256 为 `899eb64518664a726612a3e6e3e9a13037c4160ae9342ba21f08468fb5c53ac0`。在空存储目录和独立服务端口用该快照启动后，exact count=3、内部 ID 覆盖 `[0,1,2]`、外部 payload ID 为 `doc-a/doc-b/doc-c`、metadata 与 manifest 完全一致；仓库 `QdrantVectorIndex` 成功只读查询并返回 doc-a/doc-c/doc-b 的稳定结果。未归一化向量被 `validate_vector` 拒绝。该小批证据不外推 8M 规模；当前 v1 builder 仍是全量内存路径，后续需采用 streaming/compact 路线。

为了验证工程闭环，公开的 [C-MTEB 快照镜像](https://github.com/Lsogod/everything-ent-hybrid-rag/tree/main/data/knowledge/datasets) 可在当前网络中取得，但它只有 5000 篇文档、100 条查询和 425 条 qrels。它适合冒烟评测，不能替代课程全量数据；本地实验结果也不能写成提案的最终成绩。

RocketQA 脚本中的数据和 ERNIE 权重来自 `dataset-bj.cdn.bcebos.com`。数据包已完成哈希和结构核验；本次集群已完成 BGE CUDA 编码和受限前缀四模式实验，但模型权重、8M 规模索引和 Qdrant server/HNSW 仍未形成可交付收据。按 [评测说明](README.md) 构建 BM25、向量索引和四组实验时，模型 revision、维度、前缀、截断规则和 Qdrant HNSW 参数必须随实验结果一并记录。

Qdrant 适配器和快照发布逻辑已在 `offline/vector_build.py`、`online/vector_index.py` 中实现。没有可用 Qdrant 实例时，内存向量替身只能用于契约测试，不能作为 HNSW 性能结论。

## 团队自爬测试集（进行中，未形成正式 qrels）

2026-10-29 已使用仓库的 `evaluation.crawl_web` 对公开的中国政府网执行一次有界抓取：seed 为 `https://www.gov.cn/`，同域 HTTP(S) BFS，`max_documents=30`、`max_depth=1`、`max_bytes=500000`、请求间隔 0.2 秒，并要求 robots.txt 允许。抓取结果为 30 篇文档，corpus SHA256 为 `8e6cde8dfa0647deb9d55466064d951b445eb19f13740d349ef8398b86d5ab1b`；该语料和抓取收据保存在版本库外。

从这 30 篇文档构造了 10 个查询和 97 条 BM25 候选池记录，生成了带标题、URL、片段和启发式建议的 `annotation_review.md`。启发式建议文件的 SHA256 为 `61f38c72504dfbe4ff764946d4c149a6585faa9226646b9edff7b0b629303ba8`，仅用于辅助审阅，不能当成人工真值。当前 provisional qrels 的状态是 `ai_suggested_needs_human_review`；必须由组员独立复核每条 relevance=0/1/2 后，才能运行 `crawl_web finalize` 生成正式 queries/qrels。该 30 篇抓取也不等于 Issue #6 要求的自爬 20k 有效文档。

正式标注前不得提交 corpus、annotation、queries、qrels 或 provisional runs；人工复核完成后应重新生成 manifest，记录 corpus、queries、qrels 的字节数和 SHA256，并将人工审阅者、日期和标注规则写入收据。
