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

## 当前团队任务（以 592700e 为基线）

- [x] **RFC-0002 自爬数据维护预演**。使用冻结清单核验撤回对来源查询、候选池和判定的影响，保留幸存查询划分；[数据卡](docs/contributions/rfc0002-news20k-dataset-card.md#撤回和更新)记录更正、重建与回收流程。独立隐私复核及正文发布权限仍需确认。

- [x] **RFC-0004 离线交付**。全量词法、向量与评测证据已通过 [PR #8](https://github.com/ShwStone/NLP-SearchEngine/pull/8) 合入，关闭 [Issue #6](https://github.com/ShwStone/NLP-SearchEngine/issues/6)。[PR #7](https://github.com/ShwStone/NLP-SearchEngine/pull/7) 中尚缺的读取回归覆盖单独保留，原 PR 由维护者决定如何处置。
- [x] **RFC-0003 在线接线**。[PR #10](https://github.com/ShwStone/NLP-SearchEngine/pull/10) 合入启动身份、Router 接口、HTTP 请求保护、可观测性与 UI 身份面板，关闭 [Issue #5](https://github.com/ShwStone/NLP-SearchEngine/issues/5)。已有两套真实索引的[工程验收](docs/contributions/rfc0003-online-acceptance.md)。
- [x] **RFC-0002 共享集复建与词法参考**。重建 [DuReader 100k v1.0](https://huggingface.co/datasets/ShwStone/dureader-mini-100k/tree/v1.0)，逐文件复核发布哈希。手写 BM25 与 BM25S 在冻结的 1,020 条 dev 查询上逐排名一致，保存了两份深度 100 的 TREC run、输入校验和四项配对区间，详见[资源记录](evaluation/RESOURCES.md#dureader-100k-协作开发集词法对照)。
- [x] **RFC-0002 语义与规则 Router 基线**。DuReader 100k 的 1,020 条开发查询已完成 BM25、ANN Dense、固定 RRF、规则 Adaptive 与同源精确 Dense，另有 1,152 次同服务交错计时。质量、请求与 TREC 逐条核验，保留配对区间及 Router 的负结果。见[报告第 9 节](REPORT.md#9-协作开发集与-router-对照)；808 条全量留出另列。
- [x] **RFC-0002 本地 20k 演示验收**。从已交接的快照恢复 20,000 个点，校验同一语料的压缩词法索引。真实浏览器走四模式，实测无效语法清除旧结果。HTTP 后端断开与恢复分别通过 10 项和 27 项检查。
- [x] **RFC-0002 20k 路由日志对账**。50 条开发查询逐一运行四模式，200 条 HTTP 结果均匹配同一服务的日志。固定 RRF 调用向量 50 次，规则 Router 调用 9 次，无回退。单次顺序工程测量见[资源记录](evaluation/RESOURCES.md#新闻-20k-本地浏览器演示与恢复验收)。
- [x] **RFC-0002 配对 HTTP 复验入口**。冻结查询、执行顺序和服务身份，通过日志对账验收，导出同批排名以便独立复算质量。既有 1,152 次四模式交错计时与 2,040 次规则/线性收益对照沿用各自的原始计划和回执。见[评测手册](evaluation/README.md#paired-live-http-measurements)。
- [x] **RFC-0002 自爬数据卡与冻结集审计**。核对 20,000 篇有序文档、200 条来源绑定查询、8,281 对池化判定和四臂排名的判定覆盖。已重放历史划分，并核对新闻查询与全部 official-dev 查询的词面重叠，以及新闻正文与 8,096,668 篇官方 passage 的完整正文精确重叠。 [数据卡与复验](docs/contributions/rfc0002-news20k-dataset-card.md)
- [x] **RFC-0002 盲审裁决与正式导入**。裁决准备和 qrels 导入使用同一冻结计划及提交校验。裁决包只包含分歧条目，私有报告记录重叠一致率、分歧数及绑定哈希。见[评测手册](evaluation/README.md#human-review-of-a-frozen-candidate-pool)。
- [ ] **RFC-0002 自爬人工实测**。复用冻结的 20k 语料、划分、候选池与双索引。四份离线盲审包已回读校验，覆盖 8,281 对，其中 1,656 对有独立重叠安排。收齐四份完整评分后准备裁决、导入正式 qrels、重评四模式并报告判定覆盖和配对区间。现有辅助标注结果单列。
- [ ] **RFC-0002 公开 20k 部署复验**。部署负责人切换获准发布的冻结双索引后，用[验收命令](docs/contributions/rfc0003-online-acceptance.md#公开部署切换后的发布身份复验)验证索引、编码器、同一服务实例、四种检索模式和错误路径。
- [x] **RFC-0001 两动作对照**。完成规则与同预算随机对照、线性收益模型独立训练和等预算回放。固定训练阈值与规则在 100k 开发集另有 2,040 次真实 HTTP 对照，向量调用为 715 对 748，质量与耗时证据支持保留现有默认规则。详见 [Router 对照](docs/contributions/rfc0001-router-budget.md)。
- [x] **RFC-0001 开发集预算曲线与决策复核**。在同一批冻结排序上复算多个向量调用预算，并比较同预算随机期望。重排质量保护的新版收据核对同一开发集、同一策略汇总和独立固定深度对照，只记录开发决策。 [预算曲线](docs/contributions/rfc0001-router-budget.md#开发集向量调用预算曲线) · [重排决策](docs/rfc0001/DUREADER_QUALITY_GUARD.md)
- [x] **RFC-0001 两动作 Router 特征消融**。四组特征逐组移除，每个变体仅用 172 条训练查询重拟合，在 1,020 条开发查询上按 748 次向量调用预算配对评分。已记录误触发、漏触发和质量区间。 [特征消融](docs/contributions/rfc0001-router-budget.md#等向量调用预算下的特征消融)
- [ ] **RFC-0001 独立冲突处理待评审**。从组员 PR #9 的 `c21ddef` 合并主仓库 `592700e`，六个冲突路径的独立候选已推到 fork。由 PR #9 作者或维护者确认后更新该 PR。 [冲突处理候选](https://github.com/HenryZhang0707/NLP-SearchEngine/commit/273404cbdcc177687a18fef6272f0d58c42eb6d5)
- [x] **RFC-0001 Router 与线上接口联合验证**。隔离候选结合 PR #9 的 `c21ddef` 和 PR #11 的 `fb3a712`，保留策略工厂、服务身份、显式约束、故障回退及有界排序。保留单一向量配置入口，Qdrant 的实际 HNSW 参数与清单不符时拒绝启动。真实小型 Qdrant 的 `hnsw_ef=128` 请求载荷、结果排序及索引身份已在上一版隔离候选校验。 [Router PR](https://github.com/ShwStone/NLP-SearchEngine/pull/9) · [评测 PR](https://github.com/ShwStone/NLP-SearchEngine/pull/11)
- [x] **RFC-0001 全量词法分片与候选核对**。PR #9 的 81 个分片覆盖 8,096,668 篇，连续偏移及拼接语料 SHA-256 已核对。队友还记录了分片与已交付 compact BM25 的 train/dev 候选一致性。 [分片说明](docs/rfc0001/FULL_SHARDED_PLAN.md) · [全量报告](docs/rfc0001/DUREADER_FULL_FINAL_REPORT.json)
- [x] **RFC-0001 独立固定预算对照**。队友在 1,020 条 dev 查询上独立执行固定 CE 对照。100k 的学习策略相对固定 CE-24 的 nDCG@10 差值为 `+0.00653`，分组 95% CI 跨零；8M 相对固定 CE-33 为 `-0.01708`，分组 95% CI `[-0.02450,-0.01022]`。离线质量保护选择固定 CE-33，因而没有保留路由节省。该报告未使用 808 条留出，也不代表线上默认策略已改变。 [质量保护决策](docs/rfc0001/DUREADER_QUALITY_GUARD.md)
- [x] **RFC-0002 ANN 参数敏感性**。在同一 100k 快照和冻结 dev 上完成 Dense 与固定 RRF 的 ef=128/256 配对对照。记录四项指标、查询级区间、候选排名差异和旧 ef=256 TREC 前缀对账。 [结果与复验](docs/contributions/rfc0002-ann-ef-sensitivity.md)
- [ ] **RFC-0001 历史原生结果复跑**。向实验负责人取得固定版本的 M3 模型清单和原生特征加载器，核对哈希与运行环境后重跑对应 HTTP 确认。排序校准和增强特征实验各自需要原始依赖。 [复核边界](docs/contributions/rfc0001-final-router.md#复核入口)
- [ ] **RFC-0001 CE 策略集成与验证**。[PR #9](https://github.com/ShwStone/NLP-SearchEngine/pull/9) 承接 [Issue #3](https://github.com/ShwStone/NLP-SearchEngine/issues/3)，与当前主线存在冲突。与负责人对齐在线接口、CE 固定预算和等成本基线，保留实际 HTTP 质量、成本与误差；策略冻结后再在隔离留出集验证。两动作向量对照不替代这部分。
  - [x] 联合候选保留现有 Router、显式约束和 BM25 加速路径；HTTP 回归验证开启 CE 时使用相同的 RRF 候选前缀、先重排再分页，并对显式约束跳过 CE。
  - [x] 隔离候选中接入组员 BM25 收益策略，并用真实模型、同一 100k 索引验收固定 RRF/CE28 的公开 HTTP 路径。
  - [x] 实现训练集专用的固定 RRF/CE40 采集和拟合入口；学习门控绑定索引、检索设置及 CE 模型，HTTP 回归覆盖触发、跳过和约束绕过。
  - [x] 在 100k 的 172 条训练查询上完成真实 HTTP 采集和门控拟合，回读了计划及六份产物。真实服务小样本覆盖门控触发 40 对、主动跳过和显式约束绕过。训练回执 SHA-256 为 `e5f8dafe5421281e23639f4360d5c53830e16b285cbb0e621c4af871ac2fd124`。
  - [ ] 用 1,020 条开发查询比较 RRF、固定 CE28/40、学习门控与实际 CE 评分对数相等的随机对照，保存同批质量、耗时与分支证据。
  - [ ] 在交接过的 8M 双索引上，按同一协议和原训练划分另拟合全量门控，冻结后再评 808 条独立留出。100k 仅覆盖这批留出的 68/4,054 条判定对应文档，不作为其替代语料。分开报告两种索引上的质量、HTTP 耗时和错误分析。
- [x] **Windows 回归验证**。修复 checkpoint 子进程退出后锁释放的时序断言、CLI 子进程编码、原生路径断言和 JSON 解码异常的测试稳定性。完整 Windows unittest 运行 211 项，跳过两项 POSIX 专用用例，其余通过。
- [x] **旧 PR 功能核对**。[PR #2](https://github.com/ShwStone/NLP-SearchEngine/pull/2) 的采集与界面改动、[PR #7](https://github.com/ShwStone/NLP-SearchEngine/pull/7) 的离线构建与读取能力已有覆盖。保留通用 SentenceTransformer 的批次配置、Qdrant 逐批写入及失败后仅清理新集合的回归。核心测试覆盖 PackedIndex 缓存淘汰、同词并发合并、读失败重试及启动失败后的文件和 SQLite 句柄清理。

## 证据边界

- 官方公开基线的判定来自 dev。808 条留出查询来自该集合。开题报告中的 100001/2000/9839 是团队提出的目标快照，来源见 [资源记录](evaluation/RESOURCES.md)。
- 2026-11-13 [公开演示站点](https://search.shwstone.cn/)的 `/health` 返回 200，`/index/info` 仍回显 `tiny`、10 篇、`retrieval.vector_ready=false`。本地 20k 验收使用独立服务。
- 自爬评测的判定来源为 assistant 全文复核，报告按工程评测解释。数据、模型、快照、索引与生成结果均保存在 Git 外。

## 复验入口

```bash
python tests/run.py
python -m pip install ijson==3.4.0.post0
python -m unittest discover -s tests -p 'test_*.py'
python -m unittest discover -s evaluation -p 'test_*.py'
node --test tests/test_web.cjs
python -m compileall .
git diff --check
```

规模构建、恢复、合并和产物校验命令见 [RFC-0004 规模构建](docs/RFC0004_SCALE.md)。
