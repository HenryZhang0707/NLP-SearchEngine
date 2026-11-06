# RFC-0004：离线产物交付与校验补充

本文件记录 RFC-0004 的产物发布与校验入口。PR #7 与 PR #8 都包含部分
streaming、compact 和 packed 代码。整合时沿用共同的索引格式和读取契约，
以最终集成验证确认两条分支的改动。

## 已落地的交付语义

1. `offline.build` 现在先在目标目录同文件系统的临时目录中构建，完成 A1 文件和
   manifest 后再原子改名；已有非空目录不会被覆盖。JSON 错误、字段类型错误和重复
   外部 ID 在写出前拒绝；空标题或空正文仍按既有 A1 兼容语义保留。
2. `offline.build_hybrid` 在新构建失败时删除本次创建的目录；已存在的发布目录不被
   这个入口覆盖。成功构建会生成 `build_provenance.json` 与 `build_report.json`。
3. provenance 记录数据文件名、字节数、SHA256、ordered corpus fingerprint、文档数、
   BM25 参数、完整 embedding 配置、collection、backend、index params 和快照摘要，
   不写入机器绝对路径、凭据或 qrels。
4. build report 记录各阶段耗时、每个产物的大小和 SHA256。峰值 RAM/GPU 若没有作业
   监控证据就保持为空，不用估算值冒充实测。
5. Qdrant writer 逐条消费记录、按 batch upsert，检查内部 docid 的连续顺序、外部 ID
   唯一性、向量契约和 exact count。创建的新 collection 在后续失败时尝试删除；已有
   collection 永不覆盖。
6. 快照下载写入 `.part` 文件，完整下载后才改名；失败不会把半截文件当作快照。
7. `python -m offline.validate --build-dir <dir>` 要求 A1 四个文件都有 SHA256，核对
   BM25/vector 的文档数和 corpus fingerprint，验证快照哈希、provenance 和 report
   中的完整必需产物集合、身份字段和产物哈希。它只依赖 offline/contracts，不导入 online。
8. writer 返回成功后若本地 manifest 发布失败，Qdrant writer 通过 `rollback(manifest)`
   尝试删除本次 collection；原始发布异常优先保留。
9. 混合构建会将 Qdrant 快照写入整包的暂存目录。使用实际 Qdrant 1.16.0、三篇
   fixture 文档及确定性测试编码器验证了发布、快照哈希和混合查询。该检查覆盖
   服务与发布流程，语义模型和全量恢复按独立流程验收。
10. 复用 [PR #7](https://github.com/ShwStone/NLP-SearchEngine/pull/7) 的轻量文档读取接线，
    编码与 provenance 生成通过带 `manifest()` 的 `DiskDocuments`，不额外物化
    词法 postings。回归在禁止构造 `DiskIndex` 的条件下完成双索引构建，随后正常
    校验交付目录；底层 A1 构建器的内存边界仍按下节说明。

## 全量 native merge / reader 验收（2026-11-06）

native supervisor、merge 和 reader 三份 receipt 均为 `PASS`，worker exit code
为 0。全量 lexical/compact 合并产物包含 8,096,668 篇文档和 8,578,589 个词项，
ordered corpus fingerprint 为
`0ae8b1174d0ee8e55f269c3c6d8e5f12d7e9c79fcb72c3966e99de18c9061019`。
冻结的 native 程序重新校验源文件与产物哈希、执行完整索引语义检查和顺序指纹检查，
并通过公开 `DiskIndex` 入口读取内部 ID 0、4,048,334、8,096,667。

root 已核对三份 receipt 的原始字节 SHA256 及绑定关系；没有独立下载并重哈希约
107 GB 输入。supervisor、merge、reader receipt SHA256 依次为：

- `bc9b425ccc15f1f87576e03c37d13b7ee6bae24b6445a3aebf6895070d5e2b59`
- `1d4704da31373db5bbb6b441a5dc23f84fe1ea3792d95057c5528ec6bb95f0f3`
- `aa008c41a3bf323e596147893d19b174db2c1b582207ed7609a8ff89756c01ad`

请求资源为 2 CPUs / 10 GiB / 0 GPUs，supervisor wall time 为 40,563.54 秒，
worker peak RSS 为 392,413,184 bytes，精确进程成员关系 cgroup peak 为
2,201,608,192 bytes；该 cgroup 并非进程独占。最终 receipt 未记录 OOM 或 failcnt
增加。旧失败与小规模 probe 保留其原有结论。本次通过的是全量 merge 与 reader
工程验收。旧 808 query 四模式评测已实际运行，但因传输错误未形成完整 arm seal。等价快路径及同环境 CPU 门禁已通过，新的 dev32→formal808 流程已冻结；正式 8M 质量仍未完成。

## 尚未宣称的能力

- 旧 v1 builder 仍会把 conformance oracle 放入内存。规模构建使用带断点的 SQLite
  构建、compact 转换和分片合并入口，见 `docs/RFC0004_SCALE.md`。
- `offline.validate` 能验证已导出的快照文件完整性和本地双索引身份，但不能把没有
  干净 Qdrant 实例恢复日志的构建伪装成“已完成恢复验收”。真实恢复仍需在隔离实例中
  执行并记录 Qdrant/server/client 版本、collection metadata、exact count、ID 覆盖和
  corpus fingerprint。
- 失败后的默认语义是使用新的版本化 collection 全量重建；没有实现跨 corpus、模型或
  embedding 配置的断点续写。任何 retry/resume 都必须先通过同一 provenance 校验。
- `memory-exact` 和 SDK double 只能验证契约，不能替代 HNSW、Qdrant 服务性能或语义
  质量实验。自爬 20k 和批准的标准数据版本必须分别记录，不能用 prefix 冒充全量。

## 运行与验收

```bash
python tests/run.py
python -m unittest discover -s tests -p 'test_*.py'
python -m unittest discover -s evaluation -p 'test_*.py'
python -m offline.validate --build-dir /path/to/released-dual-index
python -m offline.validate --lexical-index BUNDLE/compact --vector-artifacts QDRANT_DIR
```

所有数据、模型、Qdrant collection、快照和生成 run 均保持在版本库之外。
