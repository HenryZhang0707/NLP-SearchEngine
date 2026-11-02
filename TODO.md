# 交付 TODO（2026-11-02）

这个清单把已验证事实、可继续执行的工作和受外部资源阻塞的工作分开。开题报告中的数量和方向只作为团队假设；实际资源、脚本输出和标注状态以 `evaluation/RESOURCES.md` 及本清单为准。

## DONE

- [x] BM25、TF-IDF、倒排/磁盘索引、布尔/短语/位置过滤、稳定 Top-K、高亮、分页和 HTTP v2 契约。
- [x] Router、RRF、RM3、lexical/dense/hybrid/adaptive 四种模式、向量失败时的显式 503/fallback telemetry。
- [x] 官方 RocketQA DuReader-Retrieval baseline archive 的哈希、分片、ID、dev 查询和 qrels 校验；确认它不是课程报告所写的 100001/2000/9839 快照，也没有官方 test labels。
- [x] 集群上固定 prefix20k、真实 SentenceTransformer revision 和 qdrant-client local exact backend 的四模式工程冒烟及 query-level paired bootstrap；结果只代表 19 条有标注查询，不能外推为全量或 HNSW 结论。
- [x] `evaluation.evaluate` 的完整 MAP@100 召回深度、路由 telemetry 与 `evaluation.compare` 的确定性 paired bootstrap。
- [x] 公共演示站点 [search.shwstone.cn](https://search.shwstone.cn/) 的 lexical 搜索和 adaptive 缺向量时的明确 fallback；修复前端失败后清除旧路由/耗时观测的残留问题。
- [x] 新增有边界的 `evaluation.crawl_web`：HTTP(S) only、robots-aware、同域、深度/文档数/响应大小/速率限制，以及人工标注后生成 queries/qrels 的校验入口。
- [x] 执行一次可审计的公开站点抓取：gov.cn、30 篇文档、10 个查询候选池、97 条 provisional 建议；收据、文件哈希和审阅模板均保存在版本库外。
- [x] 复跑同域 depth=2 的扩大抓取并记录实际结果：393 篇有效文档、corpus SHA256 `757e93bfc08f800827cff925864155032bbfe5d4be6cdcc5009b7b8996419531`、10 查询/100 条 provisional 候选；未把请求上限误报为实际文档数。
- [x] 修复 HTTP 分块传输中断并完成 depth=3 重试：799 篇有效文档、corpus SHA256 `1f209cad5da40808b14f6249b886308e977f708dd222f2c9eaf984f3d5b9f258`；仍只有未标注模板，未形成正式 qrels。
- [x] 完成 depth=4 有界重试：1,029 篇有效文档、corpus SHA256 `463ac4eff4fdc22047da362fdc22d47f0be6bf09c1587c3c8ba318b9e8164ff3`；生成 10 查询/100 条 provisional 候选和人工复核表，仍未形成正式 qrels。
- [x] 用 SQLite 流式构建器和 compact reader 对 depth=4 的 1,029 篇语料完成规模路径冒烟；stream/compact 产物保存在版本库外，不能外推为 8M 验收。
- [x] 从公开 sitemap 增加 108 篇页面并与 depth=4 语料按 URL 合并为 1,137 篇；在用户明确授权后完成 100 条候选的 assistant 语义复核，生成 10 个 queries、100 行正式 assistant qrels，并完成 BM25、RM3、adaptive fallback 和 paired bootstrap 工程评测；结果不等同独立人工 gold 或 20k/8M 成绩。
- [x] 以相同 robots、同域、深度和速率约束对新华网完成 100 篇跨站点抓取试验；输出仅为未标注候选模板（corpus SHA256 `eafb1dee8ddc36a3d5c778883d8e379618d073014d742359623cb321db8394f5`），未将其误作正式 qrels 或质量结果。

## NEXT（需要继续做，但不应伪造结果）

- [x] 恢复并核验本地官方 archive，全量准备 8,096,668 篇文档、2,000 dev queries、9,839 qrels；数据准备改为原子发布，修正历史摘要误抄。
- [x] 修复真实语料 `()` 的零 token 兼容问题并保留全部 ID/行序；增加可选事件流 compact、严格源哈希、分盘 SQLite scratch 和专项回归。
- [x] 完成官方 shard 01 的 10k 资源 pilot 和磁盘补测；结果用于容量预估，未冒充正式评测或全量成本。
- [~] **RUNNING（本地 CPU）**：首个非检查点 8M 进程已异常终止，旧 journal-OFF DB 已确认损坏并保留。新 `offline.checkpoint_build` 用 WAL/FULL、文件长度/输入 offset 事务检查点恢复全量 lexical；compact、全文件 SHA256、数量、ID/offset/reader 验收仍待完成，启动不算交付。
- [x] 构建检查点 hard-exit/发布/绑定及首次空库恢复回归通过；完整 official-dev 内部划分已固定为 train/dev/holdout=172/1020/808 queries，并保留来源及已观察过前缀查询的边界；HNSW 建图和显式搜索配置也已在正式质量运行前冻结，实际图就绪仍待验收。
- [x] 持久 raw 抓取、固定正文清洗、原响应字节缓存、精确/近重复和 sidecar 已实现；原 raw 任务以 2,015 篇状态停止并保留，新 `crawl_articles` 直接以有效唯一文章为目标运行，不复用旧 qrels。前五篇真实 capture/正文/metadata 重算一致，150 篇 snapshot 未发现规则内残留近重复。
- [~] **RUNNING（本地网络采集）**：继续采集至清洗去重后 20k，冻结 corpus/sidecar/receipt 和 SHA256，再设计 150–300 查询、盲化多系统池化并以明确 assistant provenance 逐条复核。当前运行与抽样不算最终验收。
- [x] 可恢复 `checkpoint_compact`、compact 自动 reader 接线及严格元数据校验已实现；相同真实 10k pilot 的六个数据文件字节、384,350 词项逻辑行和 28 篇抽样一致。源码审查发现首次空库启动窗口并补严格恢复；全量 compact 仍须等 lexical 发布。
- [x] 通过仅驻留内存的非 GUI API 会话核对现有任务和剩余 CPU/RAM/型号配额；新隔离 CPU2/RAM2GiB/GPU0 源码预检在真实 Python3.10.14 / Torch2.3.0 环境通过核心、evaluation、56 项专项及编译，耗时 52.90 秒。未修改已有任务，不使用电脑控制；后续每次提交仍须刷新容量。
- [x] 集群标准语料原始 SHA/大小/行数重新核验通过；固定模型六个官方文件完成本机下载、集群传输和回下载 SHA 验证。34 个依赖 wheel 经官方 PyPI hash/size 和原生 Linux 依赖闭包核验，离线安装成功；旧缓存缺文件、直接下载超时及安装阶段超时证据均保留。
- [x] 共享固定 BGE 已通过显式本地路径接入 online server，含 inventory 验证、FP32/CLS/right 和清晰失败路径；无模型接线回归通过。
- [x] 新增校验原始 SHA/文档数的 `CorpusDocuments`、最小共享 `DocumentSource` 和向量 CLI，允许冻结 corpus 的编码与词法构建并行；有序 fingerprint 算法和公共文件格式不变，输入先于模型校验，含 Windows 文件变化/关闭/并发及真实小语料对齐回归。
- [x] 实际全扫 8,096,668 行冻结 corpus 的文档源准入通过：108.55 秒、峰值工作集 208.44 MiB，固定首/中/末 ID 可读且 reader 已关闭；未重复全扫。此结果不替代外部 ID 唯一性、完整双索引或语义质量验收。
- [x] 显式 `--hnsw-ef` 接到 Qdrant `SearchParams(exact=false)`，默认行为兼容，非法/不适用配置在加载前拒绝；当前只证实参数接线，真实图构建、planner 路径、快照恢复和质量尚待验证。
- [x] 修复 Qdrant writer 只保存 HNSW 参数 metadata、却未将参数传入真实建库请求的缺口；非空参数严格传入并在快照前核对服务实际配置，忽略/错类型/缺字段不发布。默认空配置与旧客户端替身保持兼容，7 项专项回归通过。
- [x] 修复向量产物归属保护：SQLite 使用独占临时目录，snapshot/manifest 以不覆盖既有目标的 hard link 发布，拒绝跨平台非法快照文件名与截断下载；Qdrant 创建冲突、超时或显式拒绝不触发删除，已确认本次创建后才清理。15 项专项通过；不支持 hard link 的文件系统安全拒绝发布。
- [x] 固定 BGE 的真实 CPU 数值准入通过：Torch2.3.0 / Transformers4.46.3，FP32、CLS、right/512、长短文本、padding、两共享实例及重复编码；最大绝对差 `1.7753e-7 < 1e-5`，峰值 RSS 743,702,528 bytes，全部 dependency/model/source hashes 保留。它不是语义质量评测。
- [~] 为 4090 / 2080Ti 各提交一个隔离的同版 2,048 文档 CUDA 数值/吞吐 pilot（每任务 1 GPU / 4 CPU / 8 GiB）；提交已确认，最新 pod 仍为 Pending、无已启动容器，平台同时显示 Scheduled/Failed，尚无 inference 收据。保留任务句柄，只读检查调度事件，不重复提交或影响已有任务；通过 frozen tolerance 后再决定全量分片/缓存。提交或 CPU PASS 不等于 GPU PASS。
- [x] 预检完整 2,000 queries 无语法错误，其中 3 条显式约束预期被 dense 契约拒绝；新增默认严格、query SHA 绑定的显式 expected-unsupported policy 和可追溯请求日志，保留全部查询的指标分母、单列覆盖率和失败延迟，意外错误仍中止。正式 policy 留在 Git 外，尚未运行正式质量请求。
- [x] 规模恢复/清洗批次已集中提交 `4e8ed60`、仅推送 fork，更新现有 PR #8 / Issue #6；后续文档源/HNSW 接线及完整规模结果继续积累，不为状态变化单独建 PR。

- [x] 标准语料使用已核验的官方 8,096,668 篇版本；全部 official-dev 查询的内部划分、ID 清单和 SHA256 已在全规模质量运行前冻结（train/dev/local-holdout=172/1020/808）。100001/2000/9839 只是团队提案假设，不作为教师指定快照；不声称拥有未公开的 official test labels。
- [ ] 在 dev 上冻结 Router 阈值、candidate pool、RRF `c` 和模型配置，再在独立 test 上运行 lexical、dense、固定 hybrid、adaptive 四组；逐查询保存 TREC run、路由原因分布、向量触发/成功/fallback、平均/p50/p95 延迟。
- [ ] 用 `evaluation.compare` 至少完成 fixed-hybrid vs adaptive 的 paired bootstrap，并在报告中给出 query-level 差值和区间；不要在 test 上挑阈值。
- [~] 选择允许抓取的公开站点并运行 `crawl_web crawl` 已完成 gov.cn 的 30/393/799/1,029 篇候选批次、sitemap 合并后的 1,137 篇，以及新华网 100 篇跨站点候选；assistant qrels 已按授权生成，但仍需找到来源扩展到 20k 有效文档。若要独立人工 gold labels，必须另行复核，不能把 assistant labels 改称人工真值。
- [ ] 将经过人工确认的自爬 20k corpus、queries、qrels 和 manifest 接入统一双索引构建；保留来源、ID、计数和 SHA256。
- [x] 纳入 SQLite 支持的 `offline.stream_build`、compact/packed reader、DiskDocuments 和临时 SQLite ID 校验，并以 tiny corpus conformance 验证；1,029 文档的 stream/compact 已完成，但全量 packed conformance 在本地超过 20 分钟无收据后停止，仍需进行有界验证、8M 预检、全量构建、峰值 RSS/磁盘/吞吐和快照收据。
- [ ] 若要声称 HNSW 或 Qdrant 服务性能，必须另行启动可复现的 HTTP Qdrant 服务并记录版本、索引参数、快照哈希和失败率；local exact smoke 不替代这项验收。
- [ ] 公共站点当前展示的是 tiny lexical 部署；向量模式必须在后端真正提供固定模型和向量库后再宣称可用。部署变更后重新做 `/health`、`/index/info`、四模式和错误路径检查。

## BLOCKED / 证据边界

- 官方公开 baseline 只有 dev 标注，课程目标快照和 test labels 尚未被公开资源证明存在；不能把 dev 或 prefix smoke 改名为 test/full-scale。
- 本地电脑没有 NVIDIA GPU；真实 embedding/Qdrant 的集群运行产物不提交仓库，且不代表全量语义质量。
- 网络爬取只负责构造候选 corpus；本次 qrels 的标签来源明确为 assistant 语义复核，用户已授权将其作为正式工程 qrels，但它不是独立人工 gold labels。脚本不会从网页标题自动制造真值。
- 历史 IAB 导航/会话或另一入口空响应不能支持“集群不可用”或“配额不足”。本轮非 GUI API 任务查询和隔离 CPU 实际执行已成功；旧 SDK 路由不适配现平台。后续继续仅用 CLI/SSH/API，并在提交前核验资源，已有任务始终受保护。

## 固定验证入口

当前新增代码验证为核心 62/62、evaluation 74/74、checkpoint/stream/BGE/文档源/CLI/HNSW/产物保护专项 107/107，compile 和 diffcheck 通过；此前未变动的 Web 代码已通过 Node 5/5。这些是代码验证；全量文档源和真实 CPU 模型准入已有独立收据，完整双索引与 HNSW 实验仍待完成。

```bash
python tests/run.py
python -m unittest discover -s evaluation -p 'test_*.py'
python -m unittest tests.test_checkpoint_build tests.test_checkpoint_compact \
  tests.test_compact_stream tests.test_bge_serving tests.test_corpus_documents \
  tests.test_build_vectors_cli tests.test_hnsw_search_config tests.test_hnsw_build_config \
  tests.test_vector_artifact_ownership -v
node --test tests/test_web.cjs
python -m compileall .
git diff --check
```
