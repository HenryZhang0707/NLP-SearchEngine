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
`b051d3d966f7b0288aed95a923fc3b706176767d8bdcff8aa8fe5fff84b20662`；准备后的 dev 资源为 8,096,668 篇文档、2,000 条查询和 9,839 条 qrels。其 passage map、corpus、queries.dev 和 qrels.dev 的 SHA256 分别为 `9b091ee62278b02973c6fa6de5798c3b0bee9a493613ab29a4b807efdd38741a`、`bc67f3ccfe7423c039f04c7a5e7f6eae07a7f722087edf0caa7cfc694a8c45e8`、`3be3067e73022ea5b3b919196baae29dc2d50c6aa7053ad911e2b3d5cf8d389a` 和 `150f4665e779c0c876a46102d9a00fa467759847bc01ff344ee6c7f92c43fd79`。

为控制内存和验证完整 HTTP 闭环，从该 dev corpus 流式取前 20,000 篇文档，固定 seed `20261028`，按 dev/test/train=0.5/0.4/0.1 得到 7/9/3 条查询。前缀 corpus SHA256 为 `7e2a20ca5f7a75ece81efffbb45f10983c90ebaf500f60a4a53f9c775a55bfc8`；test qrels、queries 的 SHA256 分别为 `b07146daa42cd3b53c4c55779625ecce20dca3d337d86a64eb03a1c3d288f1c2` 和 `f05c990beb04a4997b7910149d4a128f74823aae6f1af7b09c84c80ec5f20ef5`。这仍是官方 dev 的前缀烟雾数据，不能替代课程目标快照，也不是团队自爬测试集。

语义编码使用 `BAAI/bge-small-zh-v1.5`，固定 revision `7999e1d3359715c523056ef9478215996d62a620`，512 维、CLS pooling、最大 512 tokens、L2 归一化；`model.safetensors` SHA256 为 `354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026`，`tokenizer.json` SHA256 为 `48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26`。运行时为 PyTorch 2.3.0、Transformers 4.57.1、NVIDIA A800 80GB；CUDA 编码结果有限、归一化范数为 1，重复编码最大绝对差为 0。标准 Hugging Face 端点在该任务中不可达，权重通过固定 revision 的公开镜像取得；镜像只用于下载，不改变模型 ID 或 revision。

路由参数在 dev 上先固定为 `candidate_k=200`、`rrf_c=60`、`min_vocab_coverage=0.7`、`min_relative_gap=0.1`、`policy_version=rules-v1`，随后只在 test 上报告结果。test 的四模式结果如下；dense/hybrid/adaptive 使用真实 BGE 查询编码和 20k 向量，但向量读取器是 `memory-exact` NumPy 全扫替身，因此不能解释为 Qdrant HNSW 性能：

| mode | nDCG@10 | Recall@10 | MRR@10 | MAP@100 | mean ms | p50 ms | p95 ms | vector success / fallback |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| lexical | 0.625659 | 0.666667 | 0.611111 | 0.626455 | 50.90 | 49.29 | 61.09 | 0 / 0 |
| dense | 0.958992 | 1.000000 | 0.944444 | 0.944444 | 198.48 | 202.67 | 218.39 | 9 / 0 |
| hybrid | 0.791378 | 1.000000 | 0.730688 | 0.730688 | 200.81 | 200.91 | 223.86 | 9 / 0 |
| adaptive | 0.713333 | 0.888889 | 0.659259 | 0.666667 | 133.46 | 169.74 | 229.49 | 5 / 0 |

配对 bootstrap 使用 test 的 9 条查询、10,000 次重采样和 seed `20261028`，差值定义为后者减前者。lexical→hybrid 的 nDCG/Recall/MRR/MAP 差值及 95% percentile CI 为 `+0.165719 [0.037037, 0.294400]`、`+0.333333 [0.000000, 0.666667]`、`+0.119577 [0.015873, 0.238095]`、`+0.104233 [0.011111, 0.222222]`；lexical→adaptive 对应为 `+0.087674 [0.000000, 0.207683]`、`+0.222222 [0.000000, 0.555556]`、`+0.048148 [0.000000, 0.122222]`、`+0.040212 [0.000000, 0.098413]`。样本量很小，CI 只证明这次前缀烟雾的配对差异，不能外推到全量。

真实 Qdrant server 小批验收已完成：服务端版本 `1.16.0`，客户端版本 `1.16.0`。仓库自身 `QdrantSnapshotWriter` 创建 `rfc4_writer_20261029b` collection，写入 3 个 4 维 cosine 向量和完整 `nlp_search_engine` metadata；HNSW 参数为 `m=16`、`ef_construct=100`。快照文件 112,640 bytes，SHA256 为 `899eb64518664a726612a3e6e3e9a13037c4160ae9342ba21f08468fb5c53ac0`。在空存储目录和独立服务端口用该快照启动后，exact count=3、内部 ID 覆盖 `[0,1,2]`、外部 payload ID 为 `doc-a/doc-b/doc-c`、metadata 与 manifest 完全一致；仓库 `QdrantVectorIndex` 成功只读查询并返回 doc-a/doc-c/doc-b 的稳定结果。未归一化向量被 `validate_vector` 拒绝。该小批证据不外推 8M 规模；stream/compact 入口已纳入，但旧 v1 builder 仍是兼容性全量内存路径，8M 资源与 HNSW 收据仍需单独完成。

为了验证工程闭环，公开的 [C-MTEB 快照镜像](https://github.com/Lsogod/everything-ent-hybrid-rag/tree/main/data/knowledge/datasets) 可在当前网络中取得，但它只有 5000 篇文档、100 条查询和 425 条 qrels。它适合冒烟评测，不能替代课程全量数据；本地实验结果也不能写成提案的最终成绩。

RocketQA 脚本中的数据和 ERNIE 权重来自 `dataset-bj.cdn.bcebos.com`。数据包已完成哈希和结构核验；本次集群已完成 BGE CUDA 编码和受限前缀四模式实验，后续固定模型文件已完成 SHA 验证及真实 CPU 准入，详见后文；8M 规模双索引和 Qdrant server/HNSW 仍未形成最终交付收据。按 [评测说明](README.md) 构建 BM25、向量索引和四组实验时，模型 revision、维度、前缀、截断规则和 Qdrant HNSW 参数必须随实验结果一并记录。

Qdrant 适配器和快照发布逻辑已在 `offline/vector_build.py`、`online/vector_index.py` 中实现。没有可用 Qdrant 实例时，内存向量替身只能用于契约测试，不能作为 HNSW 性能结论。

## 团队自爬测试集（20k 语料已冻结，完整池化 qrels 待完成）

2026-11-03，新华网 `clean-news/v1` 抓取任务达到 `TARGET_REACHED`，清洗及精确/近重复去重后共有 **20,000** 篇有效文章。该语料独立于官方 DuReader 前缀试验，采用同域、robots fail-closed、深度 6、每次请求间隔 1 秒和单响应 1,000,000 字节上限。语料与 sidecar 的最终 SHA256 已重新核验，原始响应缓存、数据和索引均不进入 Git。

| 最终产物 | 字节数 | SHA256 |
| --- | ---: | --- |
| corpus.jsonl | 84,555,357 | `3f16eaeae59ab9c0cfc80c219b0f51b8a0aeb784cca4a2be4070c0161447b851` |
| article_sidecar.jsonl | 36,800,693 | `f19c6972e6d97419a6206c47fb695757f815e8b6ef62953d4ef8d498737afc7e` |

最终回执 SHA256 为 `e73861130be4da1ee1a1eef772e4d52bbb5837126a265b6650aadebade8e99a6`。此前冻结的 200 条来源支持查询（dev/test=50/150）均与最终语料的来源 ID、标题和正文身份核验一致；查询文件 SHA256 为 `3b906bfc4841dd34f919f3b87a984a7902860348f0ce6ce40213b99b50632578`。已有 200 条 assistant 种子标注不代表完整检索池 qrels；五系统深度 20 的盲化池化及其余标签尚待完成。正式质量请求将检索到深度 100，并同时报告已标注覆盖率；有限池下的 Recall/MAP 不被表述为穷尽全语料的人工真值成绩。

同一冻结 20k 语料已完成真实 BGE/512/FP32 文档向量缓存。独立作业申请 1 张 2080 Ti、4 CPU、4 GiB，共耗时 197.097 秒，包含模型/源码准入、编码、全量缓存校验与打包；本机再次完整核验 20,000 条映射、向量和输入哈希。收据 SHA256 为 `456b3843225b0ec7b5bc819314e1846def1b51e99607cc36289d78198f674ca0`，缓存标识为 `a8402c6d95714b150ad4570baa0a61a85b1429fd02f3e1e5bd19c69647ba4c5b`。A1 词法索引也已发布，Qdrant 建图、快照恢复、完整池化及正式质量评测仍为独立验收项。缓存和索引不进入 Git。

以下为历史小批数据与工程冒烟，保留各自的来源和规模边界。


2026-10-29 已使用仓库的 `evaluation.crawl_web` 对公开的中国政府网执行一次有界抓取：seed 为 `https://www.gov.cn/`，同域 HTTP(S) BFS，`max_documents=30`、`max_depth=1`、`max_bytes=500000`、请求间隔 0.2 秒，并要求 robots.txt 允许。抓取结果为 30 篇文档，corpus SHA256 为 `8e6cde8dfa0647deb9d55466064d951b445eb19f13740d349ef8398b86d5ab1b`；该语料和抓取收据保存在版本库外。

从这 30 篇文档构造了 10 个查询和 97 条 BM25 候选池记录，生成了带标题、URL、片段和启发式建议的 `annotation_review.md`。启发式建议文件的 SHA256 为 `61f38c72504dfbe4ff764946d4c149a6585faa9226646b9edff7b0b629303ba8`；它只作为候选建议，不是正式 qrels。该 30 篇抓取也不等于 Issue #6 要求的自爬 20k 有效文档。

随后使用相同 seed、robots 和同域约束，将 `max_depth` 提高到 2、`max_documents=20000`，实际前沿耗尽后得到 393 篇有效文档；该 corpus 的字节数为 5,048,778，SHA256 为 `757e93bfc08f800827cff925864155032bbfe5d4be6cdcc5009b7b8996419531`。对这 393 篇文档重新生成了 10 查询、100 条 top-10 候选池和独立的 provisional suggestion manifest；建议仍标记为 `ai_suggested_needs_human_review`，不构成正式 qrels。随后修复分块传输中断边界并运行 depth-3 重试，得到 799 篇有效文档，corpus 字节数为 9,083,175，SHA256 为 `1f209cad5da40808b14f6249b886308e977f708dd222f2c9eaf984f3d5b9f258`；该批次只有未标注模板，尚未生成正式候选 qrels。由于该站点在当前边界下没有形成 20k 文档，Issue #6 的“自爬确认后 20k”验收仍未通过；所有 corpus、模板和建议文件均保存在版本库外。

随后运行 depth-4 重试，得到 1,029 篇有效文档；corpus 字节数为 11,681,876，SHA256 为 `463ac4eff4fdc22047da362fdc22d47f0be6bf09c1587c3c8ba318b9e8164ff3`。该批次生成了 10 个查询、100 条 top-10 候选池记录和 `annotation20k-depth4_review.md`；建议文件 SHA256 为 `5400b65c24c1a4e114df67c1cf4e9428621cde92052ec5ebe7bef5ae8beda643`，状态仍为 `ai_suggested_needs_human_review`。它仍不是人工 qrels，也没有达到 20k 有效文档。

对这 1,029 篇外部语料完成了不入库的规模路径冒烟：`offline.stream_build` 生成 1,029 文档、平均文档长度 2,945.7 的 v1 索引，耗时 66.69 秒；`offline.compact` 生成 178,504 词项的 compact-v1 产物，耗时 15.22 秒。stream manifest SHA256 为 `529b7c4576c50e59a20a031ad1e46b8d382e5455cc55a5be5f11782185574e10`，compact manifest SHA256 为 `06c71823b174f2b85ff5200eed96bfa8e0d90494ac5c7cee617bbe223fa00c07`。这些是 1,029 文档工程证据，不能外推为 8M 峰值 RSS、磁盘、吞吐或 Qdrant HNSW 性能。

2026-10-29 的集群复核未形成新的 8M/HNSW 任务收据：交互式任务表单在最长运行时间校验处拒绝提交；同一账号的训练入口可以看到自有 RFC-0004 工作空间和仓库目录，但入口文件级联选择读取私有数据时返回 504。未创建新任务，也未把该失败误写成计算资源不足或实验结果。

为扩大团队语料，读取 gov.cn robots.txt 声明的四份公开 sitemap，去重后得到 117 个 URL，实际抓取 108 篇可用页面；该批次 corpus 字节数为 602,669，SHA256 为 `49024d91182bfaf781b2b399e5a30c180edb0aa858fbe7a35e226124dea8702d`。将 depth=4 语料与 sitemap 批次按 URL 合并后得到 1,137 篇文档，corpus 字节数为 12,284,545，SHA256 为 `3bea58d3c66856001669a33f0919ddc95fabb4548535e1f3d6898c939a2f7ef9`；没有发现重复 URL。该合并语料的 `offline.stream_build` 用时 74.19 秒、avgdl 2,797.2，stream manifest SHA256 为 `e2336a9dfdd96d9070a278ccd6be7cac7bc36f5c85ff9658d4089a3a03a0b084`；`offline.compact` 用时 13.97 秒、186,263 词项，compact.json SHA256 为 `67abc2a7eb5e821666985abda1f6facb5b007e10f9b25085740e28cb73976aa2`。

作为跨站点抓取能力的补充试验，使用同一 `evaluation.crawl_web` 约束从公开新华网入口抓取 `max_documents=100`、`max_depth=2`、单响应上限 1,000,000 bytes、请求超时 5 秒和 0.3 秒间隔；得到 100 个唯一文档和 URL，corpus 大小 480,476 bytes，SHA256 为 `eafb1dee8ddc36a3d5c778883d8e379618d073014d742359623cb321db8394f5`。该批次仅生成 `needs_human_annotation` 模板，没有查询、qrels 或质量评测，因此只作为未标注候选资源收据，不能与 1,137 篇 assistant-reviewed 语料合并后宣称新的正式测试集。

在用户明确授权后，对 100 条 top-10 候选逐条进行 assistant 语义复核，标签来源明确记为 `assistant`，不是独立人工标注。复核结果为 relevance 0/1/2 = 26/56/18，生成 10 个 queries 和 100 行 qrels；corpus SHA256 为 `3bea58d3c66856001669a33f0919ddc95fabb4548535e1f3d6898c939a2f7ef9`，reviewed annotation SHA256 为 `42815d911a94477ccd112f4a397a98300fd9d51c9de6934eacd1d09b59e650bd`，queries SHA256 为 `c43b3d629af4850cced161a4123b1939a227317c94b92776f1af7d99cbb8081b`，qrels SHA256 为 `a6d47baf3cfc9f228863e1b5adcc3ba909792cc81480b7c57db3603224200bf5`。这些 qrels 可以用于工程评测，但报告必须保留 assistant-review provenance，不能改写成独立人工真值。

在这份 assistant-reviewed qrels 上，1,137 文档 BM25 lexical run 的 nDCG@10/Recall@10/MRR@10/MAP@100 为 `0.950197/1.000000/1.000000/0.918422`，平均/p50/p95 延迟为 `1014.92/869.50/1578.00 ms`。RM3 run 为 `0.945802/0.980000/1.000000/0.916014`；10,000 次 paired bootstrap（seed `20261029`，RM3−lexical）nDCG 差值为 `-0.004394`，95% CI `[-0.020271, 0.008033]`。adaptive 在没有向量后端时 5/10 查询触发向量尝试，5 次 fallback，fallback rate `0.5`；dense 请求按契约返回 HTTP 503 `vector_unavailable`。这些是 1,137 文档的 assistant-reviewed 工程结果，不是 20k 或 8M 全规模成绩。

所有 corpus、annotation、queries、qrels 和 runs 仍保存在版本库外；本次正式收据记录了 assistant 审阅者、日期、标签规则和 SHA256。若后续需要独立人工 gold labels，应另建版本并保留本次 assistant qrels 作为单独实验条件。

## 2026-11-02 全量数据恢复与规模构建

本地续传完成 4,214,306,577-byte 官方 archive 并重新核验 SHA256；经四分片、连续 ID、唯一性及 qrels 引用校验后，原子发布 8,096,668 篇 corpus、2,000 个 official dev queries 和 9,839 条 qrels。corpus 大小为 7,425,979,008 bytes，SHA256 为 `bc67f3ccfe7423c039f04c7a5e7f6eae07a7f722087edf0caa7cfc694a8c45e8`。数据仍位于 Git 外，完整准备不等于完整索引或语义实验完成。

本轮重新计算发现历史文字中 passage map、queries.dev 和 prefix20k/split 的五个摘要存在多抄字符，长度甚至超过 64 位。上文已用本轮真实 manifest 和相同 seed/行序/LF 规则重新构造的 prefix20k、test queries/qrels 摘要修正。这里只重建并核验了输入身份，没有重新运行或重新确认历史四模式实验的指标。

官方 shard 01 的 10,000-document 资源 pilot 保留了正文 `()` 的零 token 文档；索引契约和 stream/compact 均已支持该类记录，保持原文与内部行序。pilot 使用明确的合成 ID，只用于资源估计，不用于评测。stream 构建 152.93 秒、进程峰值工作集 33,714,176 bytes；事件流 compact 转换 21.24 秒、峰值 34,058,240 bytes。另一次相同输出的磁盘补测为 106.67 秒，100 ms 采样观测到 SQLite 最大 42,618,880 bytes、总 staging 最大 130,210,588 bytes；这些值不能作为 8M 实测资源成本。

全量 CPU 词法构建使用冻结源码副本和分盘临时库；初次非检查点进程异常终止且旧 SQLite 损坏，原目录完整保留。后续 `checkpoint_build` 使用 WAL/FULL 和输入/代码绑定，正在重新构建；仅实际发布并经数量、哈希、reader 验收后才登记完整交付。可恢复 compact 已在相同真实 10k pilot 上确认六个数据文件字节一致、全部 384,350 词项逻辑行一致和 28 篇 reader 抽样；此次包装脚本验证阶段 API 使用错误后补验通过，但没有保存转换成本，故不填新耗时/RAM。

完整 official-dev 的本地确定性划分固定为 train/dev/local-holdout=172/1020/808 queries（qrels=825/4960/4054），seed `20261028`；冻结文件含全部 query IDs 和 SHA256。它仍来自 official dev，且包括已观察过前缀查询，不是官方 test 或完全盲测。模型、candidate pool、RRF 和规则策略已记录；正式质量运行前另行冻结了 HNSW 建图与搜索配置：m=16、ef_construct=100、full_scan_threshold=10 KiB、max_indexing_threads=2、on_disk=true、hnsw_ef=256、exact=false。必须核验服务实际配置、索引状态和快照恢复，参数本身不证明图就绪。 原冻结的阈值 0 后经官方 1.16.0 二进制实测返回 HTTP 422（REST 最小值 10），故在任何正式质量请求前另存 v2 冻结，不覆盖旧配置；新配置 SHA256 为 `5fc1204abbd52faa5ded5f5dcfb7fd2a643c63163861213822db9644f616c899`。修正依据是接口约束，不是检索结果。

历史 IAB 导航、会话或入口观察不能扩大为“集群不可用”或“配额不足”。本轮非 GUI 内存 API 已核对既有任务、剩余配额、当前工作区节点名单和平台健康信息，并实际完成独立 CPU 任务。集群端重新验证全部 corpus/queries/qrels 的 SHA、字节数及行数，与本地冻结资源一致。既有任务未修改；用户禁止电脑控制访问集群，后续继续仅用 CLI/SSH/API。全量 Qdrant/HNSW 仍未运行。

独立文档源 `CorpusDocuments` 已实际扫描并解析全 8,096,668 行：108.55 秒、峰值工作集 218,562,560 bytes，原始 SHA/大小/数量匹配，首/中/末固定内部 ID 可读。该验收保留两 uint64 数组，总有效载荷 129,546,688 bytes；外部 ID 唯一性和最终双索引 fingerprint 仍有各自门禁。收据 SHA256 为 `12784e7d2156c756f0962eb867c8564278b7153994b26f7e6a10bec3bb558491`，不将文档访问准入称为完整索引交付。

固定 revision 的六个官方模型文件已逐 SHA 取得并传到新的隔离目录，回下载一致。旧缓存缺文件、集群直连下载超时和首次依赖安装时限超出的证据均保留。原生 Linux 从先前下载的 wheels 构造 34 包依赖闭包，每个 wheel 经官方 PyPI JSON 核对；46,498,720-byte wheelhouse SHA256 为 `d39dc737917e17ebab4f23a0085170709a6e715eca5750915a9bff4d57dac277`。不修改共享环境，Torch 保留基础镜像版本。

同一模型的真实 CPU 数值准入通过：Python3.10.14、Torch2.3.0、Transformers4.46.3，FP32/CLS/right/512/L2；覆盖短中文、标点、长输入、batch/padding、两个共享编码实例和重复编码。最大绝对差 `1.775333657860756e-7`，范数 `[0.9999999454,1.0000000735]`，峰值进程 RSS 743,702,528 bytes；worker 211.51 秒包含离线安装 147.69 秒。收据 SHA256 为 `25c9369d437f07265ef2e90a51383dfb1e7f35c55843827d34aa9d984e54ae93`。这是实际数值路径验证，仍不代表语义质量或 GPU/HNSW 实验。

独立 local-cache 2080Ti 任务已完成 `GPU_PILOT_PASS`，实测配额为 1 GPU / 4 CPU / 4 GiB；CPU/GPU 文档及查询最大绝对差 `1.6391277313232422e-7 < 1e-5`，范数范围 `[0.9999999137,1.0000000886]`。2,048 篇全文的 batch16/32/64 吞吐分别为 420.896/404.423/414.533 docs/s，计时覆盖分词、forward、归一化及 CPU list transfer，每档先 warmup 一批；worker 79.941 秒，supervisor 81.31 秒且 exit0。进程峰值 RSS 为 1,015,775,232 bytes；flat cgroup 路径缺少 exact-membership 证明，不把该 cgroup 数值当任务真实峰值。收据 SHA256 为 `ea976cf2f347c6b3b3d01b19af23101750aceced2f8c13df5d254cb32d3507dd`，supervisor 为 `9066c9eb975a4c54d5b362514bc4e56549fb344042ad06858af0176e36a84c9c`。这是数值/有界吞吐准入，不替代 8M 编码或质量评测；旧 GPU 调度任务及失败证据保留。

完整 2,000 queries 的语法预检发现 3 条显式约束（train/dev/local-holdout=0/1/2）。它们按当前 dense 契约预期返回特定 HTTP400；评测器提供显式、query SHA 绑定的 policy，仍发送全部请求，将准确匹配的预期拒绝记为空检索并单列覆盖率和延迟。默认仍严格失败，其他错误不能被吞掉；完整分母不会被共同支持子集分析替换。对应 policy 在质量运行前冻结于 Git 外，尚无正式请求或质量指标。

新的自爬版本使用 `evaluation.crawl_articles`，目标为清洗去重后的 20k accepted unique articles，不再以 raw URL 计数。原 raw 采集已停止并完整保留为 superseded 证据（2,015 raw docs）。新版本导入原 frontier 的 11,703 个 URL（seed 文件 SHA256 `69c807888876cba583abe372826e900ac0b90ba561c42bf07128895ebdd85f4a`，全部 depth=0），绑定固定 `news-cn-body-v1` 规则、NFKC/空白精确去重、SimHash64 距离 <=3 且长度比 >=0.9 的近重复政策。原 HTTP bytes 先持久化到 SHA 寻址 gzip，再清洗并记录完整 sidecar；不因检索分数选择文章。只读抽样前五篇的原字节/decoded SHA、正文、标题及全部元数据重算一致；150 篇 snapshot 中未发现满足当前规则的残留近重复。该轮抽样时采集仍在进行；最终 20k 语料随后于 2026-11-03 冻结，见上文。完整池化 qrels 仍待完成。正文/缓存无再分发授权，均留在 Git 外。

该新版本已有 200 条来源支持的 assistant 查询草案：对 220 个来源独立阅读正文，初稿接受 206、拒绝 14；独立 assistant 回读及正文核对额外拒绝 1 条，再按预冻 source rank 取前 200，未使用检索结果或 qrels 选择。草案 JSONL SHA256 为 `3b906bfc4841dd34f919f3b87a984a7902860348f0ce6ce40213b99b50632578`，类型为 explanation/comparison/policy/event/fact=63/9/35/43/50，dev/test=50/150 的 query IDs 已冻结。它们不是 qrels、独立人工 gold 或已完成测试的查询；全部选中来源已于 2026-11-03 核验最终 20k 的 source_doc_id/title/text 身份；盲化多系统池化、其余 assistant 标签及正式评测仍待完成。设计 manifest 与来源证据均留在 Git 外。

当前代码批次核心 62/62、evaluation 76/76、checkpoint/stream/BGE/文档源/CLI/HNSW 专项 109/109，compile 和 diffcheck 通过，未改动的 Web 代码此前 Node 5/5 通过。规模路径的当前边界和可复现实验入口见 [`docs/RFC0004_SCALE.md`](../docs/RFC0004_SCALE.md)。文档源、CPU/GPU 数值及小批 HNSW 收据均不替代完整双索引和全规模四模式/paired bootstrap 评测。

### REST 配置与存储检查的边界

隔离 CPU2 / RAM2GiB / GPU0 检查确认官方 Qdrant `1.16.0`（commit `0d79528705ff04def1b52842f747e5a00ab1e338a`）对 `full_scan_threshold=0` 返回 422，并明确要求至少 10。原始请求/响应收据 SHA256 为 `3c9bf01ad297ad74fe45d3156d85c1f44f625a1ea7fa12f50e055b108bfe5307`。检查脚本原预期 400 因而停在这一负例，不能宣称正向 10、graph readiness 或查询已通过；此前 Python3.10 hash API 不兼容和 JSON 配置未被发现的失败均保留，未把失败收据升级为 PASS。

服务配置文件使用 `.yaml`，并禁止静默退回默认端口/存储；正向建库须核验服务实际配置。历史检查中的 NFS 路径不作为 Qdrant 数据目录，不能泛称全部私有存储都是 NFS：后续静态 ROOT 实测类型为 LeoFS。独占 `/tmp` 的可见类型为 overlay，其 physical backing 仍为 UNKNOWN，因此不构成物理 SSD 或 8M 存储容量/耐久性准入。小型协议检查与正式规模存储验收分开，不能把输入配置或空库建成当成 HNSW 图搜索。

历史独立正向流水线已实际通过阈值 10 的 HTTP200 建库、完整 HNSW 配置/metadata、精确 count=0 和 YAML 资源设置继承；该组件回执 SHA256 为 `b7f13044f2844104b2dca05699b578043de7579a5c3f43d4ab1a588cc82d79d5`。语料/模型/版本准入也通过，随后在编码开始前的模型初始化阶段耗尽有界预算：wall 1185.58 秒、进程 CPU 11.84 秒、峰值 RSS 491,229,184 bytes，监督器在 1193.16 秒内清理本次新进程组。最终失败回执 SHA256 为 `42db086184e3ae93ce8cc26084c3c82b00f12ee42900b8dcd78e41809516a334`，继续作为失败证据保留；该次空库准入不升级为完整 HNSW PASS。

新的独立 local-cache 流水线已完成 `REAL_BGE_HNSW_PILOT_PASS`：2 CPU / 4 GiB / GPU0，Qdrant1.16，真实固定 revision BGE、512 维 FP32。A 建库与 B 新进程/空存储恢复均 exact count=2,048、全部非空 segment indexed，两次稳定观测。A/B 各 16 个非精确查询均以 hnsw_ef=256 执行，非空 segment 的 HNSW counter +16、plain/exact +0；空 segment 的 plain +16 单列，计数单位为 segment searches。恢复前后 ANN 与 exact 各自 IDs、顺序及 scores 一致（绝对容差 `1e-5`），不以 ANN/exact 的相互一致声称召回质量。快照 10,060,800 bytes，SHA256 `69f84ccf6607cd57a280010af9a2ec900f52b676b6e911e60e0773fed9394d39`，已持久化到静态 ROOT，交付引用不依赖 `/tmp`。worker 656.764 秒、supervisor 667.318 秒，`COMPLETE`/exit0/无 timeout；模型构造 314.718 秒不用于推断旧超时根因，2,048 文档编码 90.98 秒只作此次 CPU 工程吞吐。主收据 SHA256 `800b5618a0f017e8cf918e7b559cd7660f195fb09d5e52c056f8e03c97103dbd`，supervisor `ab8ead3deb2afb5853e95773ea6378e563738710cb7726d7a5bb33a695e888ca`。未使用 qrels、未运行质量评测，不替代 8M 资源验收或四模式/paired bootstrap。

独立 10k scratch feasibility 首次因 cgroup 统计路径不可解析，在 builder 前停止；43.44 秒不作为构建成本，失败回执 SHA256 `37399802366c4ea4986fee7e35cfcff31b0aae564337c5a45d035050e7708b27` 保留。repair1 原生构建已 `PASS`：2 CPU / 2 GiB / GPU0，核心构建 31.215 秒、320.359 docs/s，cgroup exact process membership（非进程独占）峰值 858,669,056 bytes，无 OOM；发布/绑定和两次重开全量 reader 均通过。overlay physical backing 仍 UNKNOWN，不据此认定物理 SSD 或全量准入。后续 100k pilot 首次在构建前被严格运行时绑定拒绝，原因为包装脚本错误地假设 Python3.10 使用 Unicode14；已依据 [Python3.10 官方 unicodedata 文档](https://docs.python.org/3.10/library/unicodedata.html) 改为严格 Unicode13，修正版 preflight SHA256 `653f423053e5dd614c88c273c6a72881e7c5444e43ba3ea5416873a3aa357cd8`。独立 repair1 的最终收据证实 A1 构建与独立校验已完成，但 CHECKPOINT_COMPACT 在原 4 GiB cgroup 的 90% 门禁停止：current 3,881,177,088 bytes、进程 RSS 45,588,480 bytes、compact WAL 1,980,566,432 bytes，OOM/failcnt 均无增长。原始响应 SHA256 为 `1c0c2e4ea772763b7719c2309919aa09e1123deb463964c012ed95dce33b08838`；URL 脱敏后的本地收据 SHA256 为 `557e505973b53c19cd7d0e8e776caa9cf84102b6e8bff8bc575602d3dafaba7a6`。未完成静态交付，不能声称已有持久化 100k A1。

源码和真实 SQLite 对照证实 compact 同连接的长生命周期 SELECT 阻止 WAL checkpoint；有界 keyset 分页并在写入/提交前关闭游标后，tiny WAL 峰值由 659,232 降至 32,992 bytes。旧 compiler 在新增回归中 4 项失败（database table is locked），修复后通过；相同 tiny 输入的 8 个公开产物逐字节相同，分页边界 fsync/commit 硬退出恢复保持字节一致。修复保持 WAL/FULL、原提交节奏、格式、字段校验和原生资源门禁。修复后以相同 2 CPU / 4 GiB 通过 100,000 文档原生复验。A1 构建 602.202 秒，compact 358.180 秒，cgroup 峰值 2,408,161,280 bytes，OOM 与 failcnt 均为 0。13 个静态文件读回 SHA256 与源一致，packed reader 两次重开。原始收据 SHA256 为 `6fc41aa4d19e3506ac0dc00f00faa4384ef3486cdd1664696a152692906c765be`。

100,000 文档的语义 pilot 使用 1 张 2080 Ti、4 CPU、8 GiB 和固定 BGE revision。CUDA 编码 245.574 秒（407.209 docs/s），Qdrant 1.16 完成建图、三份静态交付物的 SHA256 读回和新实例恢复。16 条探针恢复前后的 ANN 与 exact top-200 各自一致，非空 segment 记录到实际 HNSW 搜索。cgroup 峰值 2,799,603,712 bytes，无 OOM。收据 SHA256 为 `edc2a955974b0457163654d9e88729a9c20841e0416eebe9f158f978eb91f59d`，快照 SHA256 为 `e9c6d8764bc39f8f0cf086c2a67325eba4ff0aafd3be834afcea79565611eacd`。该轮未使用 qrels。

2026-11-03 另申请 2 CPU / 4 GiB 验证 100k A1 产物直接写共享卷、SQLite 留本机的布局，五个文件与已有原生私有盘基准逐字节一致。A1 构建 631.627 秒，cgroup 峰值 1,387,294,720 bytes，OOM/failcnt 均为 0；私有/共享目录采样峰值分别为 779,751,424 / 811,275,616 bytes。旧私有盘构建为 602.202 秒，两者不是随机交错的性能比较。收据 SHA256 为 `1bf2c4fd513e33b297eab85078070acf190d452f4840b51fc241c9c8543bb76e`。该次观测到的 cgroup CPU quota 相当于 4 核。试验只覆盖 A1，不能外推为全量或并发容量验收；物理后端仍未确认。
