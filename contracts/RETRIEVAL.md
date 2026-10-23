# 双索引与混合检索 API 契约

本文件是跨组接线说明；类型和校验以 `reader.py`、`index_format.py`、`retrieval.py`、`api.py` 为准。A1 词法格式 v1、A2 向量格式 v1、HTTP 响应 v2.0 分别独立版本化。

## 1. 责任边界

| 阶段 | 离线组 | 在线组 |
|---|---|---|
| BM25 | 构建倒排表、位置、tf/df、长度统计、参数与分词器哈希 | 使用统计计算查询分数，Top-N |
| 向量 | 文档批量编码，建库/近邻图，保存 ID payload，导出快照和 manifest | 查询编码，按协议只读召回 |
| 混合 | 保证两套产物同语料/同 ID/同编码规格 | Router 决策、RRF、分页、失败处理 |

只有 contracts/ 为跨组公共依赖；不把 SDK 客户端、数据库连接或在线路由放进离线构建器。

## 2. 接口 A1：BM25 索引

保留 `IndexReader`：`postings(term)`、`df(term)`、`doc_length(docid)`、`doc(docid)`、`vocab()`、`manifest()`，以及 n_docs / avgdl。

- docid：内部 int，按语料行号从 0 连续编号；跨两套索引保持一致。
- doc_id：非空唯一 str，用于评测输出；不得把内部 docid 和外部字符串混用。
- postings 按 docid 升序；位置严格递增；tf 等于位置数。
- `manifest.bm25 = {k1, b, idf}`；默认 1.2、0.75、log1p-lucene。此字段为词法 v1 的可选扩展，老产物缺省时使用默认值。非默认实验参数应离线记录，在线默认读取，不硬编码覆盖。
- 保留 docs.jsonl/postings.jsonl/offsets.jsonl/stats.json 的原有格式及文件校验。

## 3. 接口 A2：向量构建/读取

### 配置与产物

`EmbeddingSpec` 精确比较以下全部字段，不仅比较维度：

| 字段 | 约定 |
|---|---|
| model_id / revision | 模型标识与固定版本；不允许 latest；生产应用 commit/release 精确版本 |
| dimension | 正整数，文档/查询统一 |
| max_tokens | 按模型 tokenizer 右截断，默认 512，含适配器加上的前缀 |
| normalize / distance | v1 固定 true / cosine；L2 范数容差 1e-4，不接受零向量、NaN/Inf |
| document_template | 固定 `title + "\n" + text`，一文档一向量，不切块 |
| query_prefix / document_prefix | 字面前缀，分别由 encode_query / encode_documents 应用一次 |

`VectorManifest`：

```text
schema_version = 1
corpus_sha256             # 指纹算法见下
n_docs
embedding: EmbeddingSpec
collection                # 必须是新建、不可变的构建版本，不重用线上旧 collection
backend                   # qdrant-hnsw | memory-exact
index_params              # 适配器记录 HNSW 构建/搜索参数；正式评测时冻结
snapshot_files            # 文件名 -> SHA256；生产必须非空，内存测试可为空
```

语料指纹由 `corpus_fingerprint(reader)` 统一计算：逐内部 docid，编码 JSON 数组 `[docid, doc_id, title, text]`，`ensure_ascii=False`、紧凑分隔符，尾随换行，UTF-8 后流式 SHA256。顺序、ID、标题或正文变化都会改变指纹。

`VectorRecord(docid, doc_id, vector)`：point ID 为内部 docid，payload 保存外部 doc_id。发布前校验记录数 N、内部 ID 唯一覆盖 0..N-1、外部 ID 唯一、向量规格正确。

### 方法签名

```python
class TextEncoder(Protocol):
    spec: EmbeddingSpec
    def encode_documents(self, texts: list[str]) -> list[list[float]]: ...
    def encode_query(self, query: str) -> list[float]: ...

class VectorWriter(Protocol):
    def replace(self, manifest: VectorManifest,
                records: Iterable[VectorRecord]) -> VectorManifest: ...

class VectorReader(Protocol):
    def manifest(self) -> VectorManifest: ...
    def search(self, query_vector: Sequence[float], limit: int) -> list[VectorHit]: ...
```

- encode_documents 输出顺序与输入一致，数量相等。离线可分批调用；模板由构建器应用，前缀/模型 tokenizer/截断/归一化由编码器应用。
- writer 必须消费全部 records，创建新 collection，校验点数/ID/向量、完成快照，再返回仅补齐 snapshot_files 的 manifest。快照写入与 build_vectors 相同的产物目录，文件名不允许路径穿越。
- `build_vectors` 核验快照文件 SHA256，最后原子重命名发布 vector_manifest.json。构建失败不发布 manifest；调用方清理失败的新 collection/临时目录，不能污染旧线上版本。构建目录只能有一个 writer，不支持并发发布。
- reader 返回 `VectorHit(docid, doc_id, score)`；score 是 cosine 相似度，越大越好，不是距离。最多 limit 条唯一结果，按 score 降序、内部 docid 升序；ANN 搜索不保证与精确全扫同集合。
- 真实适配器须从已恢复的 collection 读取并核对嵌入配置/语料指纹等元数据，恢复时检查快照 SHA256；不能只信调用者传入的 manifest。
- 超时、模型/后端不可用统一包装成 `RetrievalUnavailable`。不要把任意编程错误吞成空列表。

### 接线与启动校验

```python
# 离线；encoder / writer 由实际适配器提供，真实 SDK 接入仍待实现
build_hybrid(corpus_path, out_dir, dataset,
             encoder, writer, collection="dataset-build-id")

# 在线；构建器和检索器不互相 import
engine = SearchEngine(reader, encoder=query_encoder, vectors=vector_reader,
                      config=RetrievalConfig(candidate_k=200, rrf_c=60))
```

`SearchEngine` 要求 encoder/vectors 同时存在或同时缺省。启动时核对完整 EmbeddingSpec、文档数与 corpus_sha256；不匹配拒绝启动，不降级掩盖版本错误。实际命中再核对内/外部 ID 和合法分数。未接入真实 SDK 的内存实现必须标记 memory-exact，不冒充 Qdrant/HNSW。

## 4. 接口 B：HTTP v2.0

### 请求

```json
{
  "query": "手机掉水里怎么办",
  "mode": "adaptive",
  "k": 10,
  "offset": 0,
  "scorer": "bm25",
  "expander": "none",
  "run_tag": "adaptive-v1"
}
```

- query：非空白字符串；k：1..200 的整数；offset：非负整数；不把字符串、小数或 bool 当整数。
- mode 默认 lexical。请求必须为 JSON 对象，不接受未知字段。
- lexical：仅词法分数，保留 scorer=bm25/tfidf 与旧 expander 参数。
- dense：只查询向量，不做 BM25；score_kind=cosine。
- hybrid：每次查询 BM25 + 向量，用 RRF；score_kind=rrf。
- adaptive：先 BM25，再由规则决定是否做向量召回与 RRF；执行模式回显 lexical 或 hybrid。
- 非 lexical 只允许 scorer=bm25、expander=none；不支持的组合报 400，不能悄悄忽略。
- 普通查询使用 OR 候选，未知词不会清空其他词的候选；这是与旧骨架隐式 AND 的明确变化。显式布尔/引号/括号当前未实现，一律 400；未来 parser 必须标注 explicit_constraints，adaptive 遇到硬约束走词法，dense/hybrid 不允许绕过约束。
- candidate_k、rrf_c、路由阈值和 policy_version 是服务级配置。非 lexical 必须 `offset+k <= candidate_k`，融合后的池也截为 candidate_k，然后分页。请求不允许临时覆盖池大小。

### 响应

```json
{
  "api_version": "2.0",
  "took_ms": 8,
  "parsed": {"type": "terms", "terms": ["手机掉水里怎么办"], "explicit_constraints": false},
  "expanded": [],
  "routing": {
    "requested_mode": "adaptive",
    "executed_mode": "hybrid",
    "reason": "no_lexical_hits",
    "vector_used": true,
    "vector_attempted": true,
    "fallback": false,
    "score_kind": "rrf",
    "candidate_k": 200,
    "policy_version": "rules-v1"
  },
  "timings_ms": {"lexical": 1.0, "router": 0.1, "encode": 5.0, "vector": 1.0, "fusion": 0.1},
  "results": []
}
```

这是结构示例，不是测得延迟或真实质量结果。结果字段仍是 doc_id/rank/score/title/snippet/highlights。

- vector_attempted：路由/强制模式是否选择语义分支，包括依赖未就绪时的失败尝试。
- vector_used：向量查询成功完成（即便返回空结果）；不能用它代替 attempted 估算全部调用成本。
- fallback：adaptive 语义分支失败后是否明确退化到词法；此时 executed_mode=lexical、reason=vector_unavailable。
- reason 当前值：forced_lexical/forced_dense/forced_hybrid、explicit_constraints、no_lexical_hits、low_vocab_coverage、small_relative_gap、lexical_sufficient、vector_unavailable。客户端可显示字符串，但不要依赖展示文案解析流程。
- timings_ms 五个阶段固定存在，未调用阶段为 0。took_ms 为整个请求耗时，含解析/高亮等，不能要求阶段和严格等于总耗时。
- score_kind 为 bm25/tfidf/cosine/rrf。分数同模式内排序，不跨模式直接比较。所有排序同分按内部 docid 升序。
- rank 为分页后的全局排名，从 offset+1 起。高亮仍是原文字符区间；语义命中无词面重合时 highlights 可以为空。

### 失败与能力回显

| 情况 | 状态 |
|---|---|
| 非法请求、分页超候选池、未实现的查询语法 | 400 |
| 旧 RM3/SLM 未实现扩展 | 501 |
| dense / hybrid 缺编码器/向量库，或运行失败 | 503，code=vector_unavailable |
| adaptive 语义分支失败 | 200，明确 fallback=true；评测需单列 |
| 双索引或模型版本不一致 | 启动失败，不接受请求 |

`GET /index/info`：原词法 manifest + retrieval={vector_ready, config, vector_manifest}。`GET /health` 仅报告原词法服务健康，不代表向量可用。当前 CLI 尚未接入真实编码器/Qdrant，vector_ready=false 是正确行为。

## 5. 契约验收与尚未实现部分

运行 `python3 tests/run.py`：覆盖 DiskIndex/MemoryIndex conformance、双索引批次和发布、快照校验、模型/语料错配、向量维度/归一化、Router/RRF、四模式、分页、显式语法拒绝、降级和 HTTP 状态/响应结构。

零依赖 runner 检查本仓库 schema 使用的响应子集，不是完整 JSON Schema 引擎。接入 UI/评测器时可用标准 JSON Schema 验证器直接消费 api.py 的两个 schema。

待填桩：`offline.vector_build.QdrantSnapshotWriter`、`online.vector_index.QdrantVectorIndex`，以及实现 TextEncoder 的真实 embedding 适配器。现在通过注入假编码器与 MemoryVectorIndex 验证编排；没有真实模型推理、真实 Qdrant 建库或 HNSW 性能结论。
