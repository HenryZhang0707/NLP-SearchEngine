# 中文自适应混合搜索引擎实现报告

## 1. 项目目标与边界

本项目面向 NLP 课程大作业，提供一个中文检索系统：离线构建手写倒排索引，在线用 BM25 排序，并通过可选的 embedding 与 Qdrant 向量库补充词面不重合的结果。系统支持 lexical、dense、hybrid、adaptive 四种模式。自然语言查询默认使用词项并集；显式约束必须先在词法层过滤，语义召回不能放宽约束。

提案中的 DuRetrieval 全量实验需要单独下载语料、queries 和 qrels。仓库只保留 tiny fixture 和可复现代码，不把约 100,001 篇文档或模型文件提交进版本库。

资源核验见 [evaluation/RESOURCES.md](evaluation/RESOURCES.md)。官方公开的 DuReader-Retrieval 版本约有 809 万段落、86K 训练查询、2K 开发查询和 4K 测试查询，并要求注册后下载；这与提案中的 100001/2000/9839 课程快照不同，不能混用。当前环境访问官方 CDN 和 AI Studio 返回 HTTP 403，因此报告不伪造全量数据或语义实验数字。

## 2. 已完成实现

### 词法与查询处理

- `contracts/analyzer.py` 提供离线、在线共用的 bigram/ASCII 分词和字符偏移。
- `offline/build.py` 写入 docs、postings、offsets、stats 和带 SHA256 的 manifest。
- BM25、TF-IDF、OR 候选、稳定 Top-K 和高亮均通过 `IndexReader` 协议工作。
- 查询解析器支持 `AND`、`OR`、`NOT`、括号和双引号连续短语，使用 NOT、AND、OR 优先级。
- 短语根据倒排位置连续匹配；布尔结果作为硬过滤传入 BM25。hybrid 在 RRF 前再次过滤向量结果，dense 对显式词法约束返回 400。

### 语义与混合检索

- `contracts/retrieval.py` 固定模型 revision、维度、归一化、前缀、截断和 cosine 约束。
- `online/embeddings.py` 提供懒加载的 Sentence-Transformers 编码器，缺少可选依赖时返回清晰的可用性错误。
- `offline/vector_build.py` 的 Qdrant writer 会创建新 collection、批量 upsert、核对点数并下载带 SHA256 的快照。
- `online/vector_index.py` 的 Qdrant reader 校验 collection 中持久化的完整构建身份、真实维度／距离和精确点数，并在命中时核对外部 doc_id 和 cosine 分数。Qdrant 与客户端均要求 1.16+；旧的无元数据 collection 必须重建。
- CLI 可通过 `--vector-url` 与 `--vector-manifest` 启用同版本向量后端；不提供这两个参数时保持零依赖词法服务。

### HTTP 与界面

- `POST /search` 返回 API v2.0 的解析树、结果、高亮、routing 和阶段耗时。
- `GET /health` 与 `GET /index/info` 提供健康状态、索引 manifest 和向量能力。
- `web/` 是原生 HTML/CSS/JavaScript 界面，支持查询、模式、Top-K、结果高亮、路由和耗时展示。服务端限制静态路径在 web 根目录内，拒绝路径穿越和符号链接逃逸。

### 评测器

`evaluation/evaluate.py` 只通过公开 HTTP API 发送查询，不导入检索内部模块。它读取 TREC qrels 和 TSV/JSONL queries，输出 TREC run，并计算 nDCG@10、Recall@10、MRR@10、MAP@100、平均延迟、p50 和 p95。已有 run 可以脱离服务离线评分。`evaluation/prepare_dataset.py` 负责外部数据校验、哈希和固定划分，`evaluation/compare.py` 提供可复现的逐查询 paired bootstrap。

## 3. 验证结果

在 tiny fixture 上执行：

```bash
python3 tests/run.py
python3 -m unittest discover -s evaluation -p 'test_*.py'
node --test tests/test_web.cjs  # 可选，Node.js 18+
python3 -m offline.build --corpus fixtures/tiny_corpus.jsonl \
  --out /tmp/nlp-tiny-bm25 --dataset tiny --k1 1.2 --b 0.75
python3 -m contracts.conformance /tmp/nlp-tiny-bm25 fixtures/tiny_corpus.jsonl
```

审阅修复后，45 项仓库契约／适配器测试、13 项评测器和数据准备测试、5 项浏览器高亮测试全部通过，tiny 索引 conformance 通过。Qdrant 测试使用 SDK double 验证发布／拒绝路径；另用 `qdrant-client==1.16.0` 的本地内存后端验证了真实 SDK 元数据读写和模型版本错配拒绝，但没有据此宣称验证了真实服务的 HNSW、快照恢复或语义质量。启动服务后，lexical 搜索、`/health`、`/index/info`、浏览器静态资源和 dense 未配置时的 503 行为均已验证。历史上对 tiny 磁盘索引重复执行 1,000 次 BM25 查询测得 p50 约 0.10 ms、p95 约 0.14 ms；这只是小样本工程冒烟，不能替代 DuRetrieval 全量结果。

历史上另用公开 C-MTEB DuRetrieval 快照（5000 篇文档、100 条查询、425 条 qrels）做了外部闭环冒烟。按固定划分取 80 条测试查询，旧版 Top-10 HTTP 请求下 BM25 得到 nDCG@10=0.8065、Recall@10=0.8194、MRR@10=0.8872，平均延迟 4.33 ms、p50 3.83 ms、p95 6.65 ms。修复审阅问题时发现旧评测器默认只召回 10 条，却标记 MAP@100，因此撤下该 MAP 数值；新的默认评测会召回 100 条，并需要重新测量 MAP 和完整请求延迟。该快照不是课程要求的 100001/2000/9839 版本，这些历史数字只验证管线，不代表最终成绩。

## 4. 全量实验流程

1. 固定 DuRetrieval 数据版本、查询划分和 qrels；先在开发集冻结 Router 阈值。
2. 用同一 BM25 参数和语料分别运行 BM25、纯向量、固定 hybrid、自适应 hybrid。
3. 对每组保存 TREC run 和每查询延迟，使用评测器报告 nDCG@10、Recall@10、MRR@10、MAP@100、p50/p95。
4. 使用 `evaluation.compare` 对自适应与固定 hybrid 的逐查询差值做 paired bootstrap，并单独报告向量触发、成功和 fallback 比例。

示例：

```bash
python3 -m evaluation.evaluate \
  --endpoint http://127.0.0.1:8000/search \
  --queries data/test.queries.tsv --qrels data/test.qrels.tsv \
  --output-run runs/bm25.trec --include-per-query
```

该命令不会下载数据或模型，也不会把评测结果写入受版本控制的源码目录，除非调用者明确指定输出路径。

## 5. 当前限制

默认分词器仍是可解释的 bigram 占位实现；DuRetrieval 的真实分词对齐、预训练模型选择、Qdrant HNSW 参数和全量性能数字需要团队根据课程数据与算力完成冻结。RM3 已作为确定性的本地伪相关反馈扩展接入；SLM 仍需要外部固定模型与离线缓存，返回 501。
