# 交付 TODO

## DONE

- 搜索引擎的核心契约已有回归覆盖，包括词法索引、布尔与位置过滤、稳定分页、HTTP API、Router/RRF/RM3，以及向量失败时的错误和 adaptive 回退。
- **自爬语料**。清洗去重后冻结 20,000 篇，dev/test 为 50/150 个查询。8,281 条池化判定含 200 条已绑定身份的种子判定和 8,081 条 assistant 全文复核，来源随数据记录。A1、compact、真实 BGE 向量和 Qdrant 快照恢复已验收。
- **自爬四模式**。150 条测试查询经公开 `/search` 契约各执行一次 lexical、dense、hybrid、adaptive，共 600 次请求。TREC、路由、延迟和配对 bootstrap 已核验，详见 [REPORT.md](REPORT.md) 与 [资源记录](evaluation/RESOURCES.md)。
- **自爬数据复验与维护**。[新华网 20k 数据卡](docs/contributions/rfc0002-news20k-dataset-card.md)记录冻结数据、划分隔离、判定来源和排名覆盖。审计和撤回预演可在授权副本上只读执行；独立人工复核、正文发布权限及公开 20k 部署仍待确认。
- **官方数据**。核验公开 RocketQA DuReader-Retrieval 资源，冻结 8,096,668 篇、2,000 条 official-dev 查询、9,839 条 qrels。内部划分 train/dev/holdout=172/1020/808，holdout 在正式质量评测前冻结。`prepare_dataset --queries-only` 已在 Windows 和固定原生运行时逐字节复建六份划分文件。
- **官方全量词法**。十片合并原生终态 PASS，8,096,668 篇、8,578,589 个词项、13 个 A1/compact 文件、有序语料指纹及正常 `DiskIndex` 读取验收通过。原生收据链已独立复核；恢复耗时和历史失败边界见资源记录。
- **官方全量向量**。固定 BGE 的 162 个向量块覆盖全部文档。Qdrant HNSW 的 8,096,668 点快照已在两个独立空实例恢复。16 条固定 ANN 查询的 Top-200 与原实例一致，exact 同分边界另经全量扫描校验。收据和资源边界见 [资源记录](evaluation/RESOURCES.md)。
- **离线交付工具**。A1 和 compact 构建支持检查点，分片合并有顺序与哈希校验，`offline.validate` 可在原位核验单目录双索引或独立发布的 compact 与 Qdrant 快照。真实 Qdrant 上三文档检查通过，覆盖快照随整包发布、哈希和混合查询。失败后的清理与重试另有回归。
- **成熟 BM25 对照**。固定 rank-bm25 0.2.2，对齐 analyzer、公式与参数后验证 16 条有效查询的分数及排名，并保留默认 IDF 的差异；复现命令和收据见资源记录。当前源码在原生 Python 3.10.14 / Unicode13 上的 153 项专项 unittest 已通过。

- **全量截断统计**。固定正式 tokenizer，8,096,668 篇中 620,314 篇（7.6613%）超过 512 tokens；全量字节身份和 162 个分块的聚合已独立核验。
- **组员分支兼容性**。最新隔离候选 `e2be311` 整合本分支 `382a8cc`、PR #7 的 `2c42ce6` 和 PR #9 的 `e8876ea`，保留组员的计分加速与 Router 接口。Windows UTF8 与固定原生环境的核心均为 86/86，Windows Python3.13 的完整评测测试 90/90，固定 Python3.10 / Unicode13 / ijson 的专项 unittest 153/153；137 份源码编译及 diffcheck 通过。前端源与测试 SHA 未变，复用 Node 7/7。该候选只在本地验证，没有合回主工作树或上游，不替代 CE 或全量质量实验。

- **官方全量四模式**。完整语料的 808 查询、4,054 qrels 已完成四臂 HTTP、TREC、路由/请求覆盖、逐臂延迟及 10,000 次配对 bootstrap，并在固定 Python3.10.14 / Unicode13 独立复算 PASS。Dense 两条预定义约束拒绝按空排名计入 808 分母，其余请求均成功、无异常或回退。Dense 四项质量最高；当前规则 Adaptive 相对 Hybrid 的四项区间均为负，保留结果。混合 epoch 延迟仅逐臂报告，见 [报告第 8 节](REPORT.md#8-官方全量-808-查询四模式评测2026-11-07-完成复核)。

## TODO（维护者交接）

- [ ] 维护者评审并决定同组 PR 的合并顺序，保留 offline 与 Router 的联合测试。RFC-0004 的全量验证和隔离联合检查均已完成，统一交付到既有 [PR #8](https://github.com/ShwStone/NLP-SearchEngine/pull/8) 与 [Issue #6](https://github.com/ShwStone/NLP-SearchEngine/issues/6)。抓取/清洗实现按数据模块协作评审；实际合并和线上部署由团队单独安排。

## 证据边界

- 官方公开基线的判定来自 dev。808 条留出查询来自该集合。开题报告中的 100001/2000/9839 是团队提出的目标快照，来源见 [资源记录](evaluation/RESOURCES.md)。
- 公开演示站点当前使用 tiny lexical 索引。部署更新后的验收单列 `/health`、`/index/info`、四模式和错误路径。
- 自爬评测的判定来源为 assistant 全文复核，报告按工程评测解释。数据、模型、快照、索引与生成结果均保存在 Git 外。

## 复验入口

```bash
python tests/run.py
python -m unittest discover -s tests -p 'test_*.py'
python -m unittest discover -s evaluation -p 'test_*.py'
node --test tests/test_web.cjs
python -m compileall .
git diff --check
```

规模构建、恢复、合并和产物校验命令见 [RFC-0004 规模构建](docs/RFC0004_SCALE.md)。
