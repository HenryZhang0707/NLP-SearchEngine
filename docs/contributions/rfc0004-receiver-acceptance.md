# RFC-0004 接收机验收与维护者补丁

接收验收日期为 2026-11-08 UTC，审阅提交为
`dfa697687ddf1a0204d42a4c52992a0ac159212d`。维护者追加补丁于
2026-11-09 完成回归。没有更改交付包、索引格式、模型、向量或冻结实验结果。
本页只发布聚合结果，不包含语料、查询、qrels、权重、原始响应或机器私有路径。

## 接收验收范围

| 检查 | 结果 |
| --- | --- |
| 完整介质 SHA/大小 | 627 文件，69,771,702,024 bytes，PASS |
| 选择性展开 SHA/大小 | 344 文件，62,254,193,082 bytes，PASS；不是全部 843 payload 验收 |
| 官方 SSD 运行副本 SHA | 18 文件，60,491,718,738 bytes，PASS |
| 两套双索引 | 官方 8,096,668、新闻 20,000 文档；文件/快照 SHA、有序语料指纹通过 |
| 官方完整 ID 链 | 8,096,668 行词法文档与向量映射逐行一致；158 篇零 token 文档保留 |
| 固定 BGE 模型 | 六文件和 inventory 验证；真实 CPU FP32/CLS/right512/L2 数值准入通过 |
| 两套空实例恢复 | Qdrant 1.16.0，metadata/维度/Cosine/HNSW 配置、exact count、两次稳定图观测通过；indexed count 分别为 8,096,668/20,000 |
| 官方冻结 ANN | 16 条 Top-200 ID/顺序一致，最大分数差约 3e-7；非空 segment HNSW +512、plain/exact +0 |
| 新闻冻结 ANN | 16 条 Top-20 ID/顺序一致，最大分数差约 6e-7；非空 segment HNSW +16、plain/exact +0 |
| 两套索引抽样 | 各 256 篇 offsets、六个低 df 词项 tf/positions、九组独立 BM25 对照、八篇实际文档向量重编码通过 |
| 两套公开 HTTP | 四模式、分页、约束拒绝、UI/路径安全、坏参数、后端 503/fallback、重启后应用不重启的恢复通过 |
| 错版/损坏拒绝 | 七项真实库/CLI/本地副本负向检查通过；没有向活库写入 |

ANN 容差预先固定为 `1e-5`；空 segment 的 plain 搜索单列，不与非空 segment
混算。官方 ANN 对照使用旧 producer receipt 的 `A_ann` 窗口；该旧 receipt
整体 exact 验证为 FAILED，本次没有将它改写为 PASS，也没有重做全量 exact oracle。

官方 fingerprint 为
`0ae8b1174d0ee8e55f269c3c6d8e5f12d7e9c79fcb72c3966e99de18c9061019`，
新闻 fingerprint 为
`1480ab10cbefcb57bc1132871420b1f88d18ac5f260150080953f9441377a861`。
官方快照 SHA256 为
`2df5c889386db365e4d72d089f90bfb736c8a6a24d5bede4a10f727d9f0976e0`。

新闻冻结 150 查询通过公开 HTTP 实际重跑四臂、深度 100，600/600 HTTP200、零
fallback。所有 Top-100 外部 ID 与顺序和交付结果一致，Dense 六位分数最大差约
`1e-6`。另对既有官方 808-query 与新闻 150-query TREC 重算四指标和四项配对
bootstrap（10,000、seed=20261028），与交付值在 `1e-12` 内一致。
官方 Dense 四项最高、规则 Adaptive 相对 Hybrid 四项区间均负的结论保持不变。

没有在接收机重跑 808×4 正式 HTTP。808 来自已观察的公开 dev 内部分割；新闻标签
来自 assistant 全文复核，不是独立人工 gold。没有调阈值、新盲测或跨 epoch 因果时延结论。

## 合并前修正

- `offline.prepare` 在 error/skip 两种策略下都保留合法、非空但零 token 的正文，
  不再因 analyzer 无 token 删除记录。空白正文和重复外部 ID 的原有约束仍保留。
  该入口会规范化字段、重写 JSONL，skip 还能筛选其他无效记录，**只用于冻结前准备**。
  冻结后应按清单在原位检查输入，发布产物使用 `offline.validate`，不重新调用 prepare。
- 简易 `evaluation.crawl_web` 在发出后续请求前拒绝所有 HTTP 重定向，包括同源和
  robots 重定向。robots 与网页共用配置的 socket timeout、字节上限、User-Agent，
  并共享最小请求起始间隔；失败、非 HTML 和叶节点请求也计入限速。
  robots 默认失败关闭；`allow_robots_failure` 仍是显式 opt-in，401/403 仍拒绝，
  404 等既有 HTTP 策略保留。NaN/无限网络预算被拒绝。
- 长期正式采集仍使用 DurableCrawl/CleanArticleCrawl 的持久化、逐跳校验和站点级限速。
  简易入口不承诺完整 SSRF 防护或持久化恢复，非可信抓取仍需限制出网。

**断点兼容边界**：`crawl_web.py` 的整文件 SHA 参与 DurableCrawl/CleanArticleCrawl
绑定，即使 HTML parser 未变，本补丁也会改变该 SHA。已有采集断点必须用其记录的
原提交或 source archive 继续，不能拿新代码续旧状态；不得放宽源码哈希检查。
现有构建/合并/向量缓存核心与发布产物没有改动，补丁不要求重建 8M 索引或重新编码。

追加回归覆盖两种 prepare 策略、混合/全零 token 输入、连续 ID，以及 loopback
301/302/303/307/308 的同源/跨源请求拒绝、robots 限额/失败策略/缓存、逐请求限速。
新回归在未修复版本上先复现失败，修正后全套通过：

```bash
python -B tests/run.py                           # 71/71 通过
python -B -m pytest -q tests evaluation           # 315 passed, 496 subtests passed
python -B -m unittest discover -s tests -q        # Ran 153 tests / OK
python -B -m unittest discover -s evaluation -q   # Ran 91 tests / OK
node --test tests/test_web.cjs                    # 7 pass, 0 fail
```

环境为 Python3.10.14 / Unicode13、Torch2.3.0+cpu、NumPy1.26.4、
Transformers4.46.3、tokenizers0.20.3、qdrant-client/server1.16.0、ijson3.4.0.post0。

## 部署门禁与证据保留

HDD/Btrfs 上快照恢复成功，但与启动文件校验并行时首条 ANN 超过客户端 30 秒，
服务端随后记录 60 秒超时。该场景不放行。NVMe/ext4 新目录使用同字节产物、相同
预算/检索参数/超时，并在无并行启动哈希任务时通过上述固定探针和 HTTP。
这是存储及负载时序同时变化的验收，不能解释为纯磁盘因果加速比。

接收测试仅 loopback、CPU 编码。官方 Qdrant/应用分别采用 20/6 GiB memory max、
各 2 CPU quota、无任务内 swap；这是该次有界测试配置，不是通用最低资源或并发容量证明。
合并不批准公开上线，仍须补限并发/队列、真实后端 readiness、规范化搜索配置回显、
观测与回滚。`/health` 是词法索引 liveness，不能替代向量后端 readiness。

原始成功、失败、请求和资源 receipts 保留在接收方受控存储，未提交 Git。
接收验收时 completion JSON SHA256 为
`6a591d76fe7cabf6f6f80e62170900466a7a30e2027cd77cb9f0f3dc4f729a31`；
point-in-time evidence inventory SHA256 为
`c731667170087e0292d59b2a20004bf122e60c0f2b6890ef1c91c5a21c0359bd`。
运行中的本地服务日志可继续追加，不应误当作不可变 release inventory。
本次没有完成全部 843 payload 验收，不授权清理任何原始交付、缓存或失败证据。
