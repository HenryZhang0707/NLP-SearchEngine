# RFC-0003 在线服务验收（issue #5）

本页记录 issue #5 / RFC-0003 在线服务化在**本机真实产物**上的一次工程验收：启动身份、
四模式 HTTP、后端失效与恢复、独立 HTTP 评测器接线、资源护栏和进程清理。全部原始证据
（验收 JSON、评测 run、请求事件、服务日志）留在仓库外的临时目录，不进入 Git。

范围边界先说清楚：这是**接线验收**，不是质量、SLO 或容量结论。新闻 20k 与官方 8,096,668
各只用 5 条冻结 dev 查询做四模式冒烟；指标数值只证明评测链路可用，不能当作检索质量证据。
没有重跑 808×4 正式实验，没有并发/吞吐/p95 结论，也没有验证认证、TLS 或多进程部署。

## 1. 代码与提交

| 角色 | 提交 |
|---|---|
| 集成分支基线 | `604d734fa1a450f75e4030035543f39f0bb70128` |
| C1 router/engine/vector seam | `0e63e670c49632acac33e600c2622a03ab061d44`（集成后 `83fb570`） |
| C2 ServingConfig + HTTP 观测 | `9ec859026ee9006ee2a2983285e3c7db23079a04`（`eab37a8`） |
| C3 Web 身份面板 | `ee6341d`（`7a04187`） |
| C4 部署文档 + 公开验收脚本 | `5bc994004dad0b4eee27d86c756395782ee16a7a`（`333ddc8`） |
| 集成修正（本页第 7 节） | `20f06f4` |

所有验收都跑在集成分支上表提交之后的代码上；新增回归在修正前代码上先复现失败（见第 7 节）。

## 2. 环境

- Python `3.10.14`（复核 venv），torch `2.3.0+cpu`、
  transformers `4.46.3`、qdrant-client `1.16`、ijson、NumPy `1.26.4`。
- 系统 Python 缺 `ijson`，其历史 14 个 `test_merge_indexes` error 属环境缺失，不是通过；
  本页所有数字都来自上述 venv，没有做全局安装。
- 测试与验收命令：

```bash
PY=<复核 venv>/bin/python
$PY -B tests/run.py                                   # 核心 runner（含函数式 router seam 测试）
$PY -B -m unittest discover -s tests -p 'test_*.py'   # unittest 类
$PY -B -m unittest discover -s evaluation -p 'test_*.py'
node --test tests/test_web.cjs
$PY -B -m offline.validate --lexical-index <compact> --vector-artifacts <vector>
$PY -B scripts/online_acceptance.py --endpoint <url> --phase healthy|down|recovered ...
$PY -B -m evaluation.evaluate --qrels <qrels> --queries <queries> --endpoint <url>/search \
    --mode lexical|dense|hybrid|adaptive --fail-on-fallback --output-run ... --output-json ...
```

- 新闻产物门禁（新跑，2026-11-09）：`offline.validate`
  `…/artifacts/news20k/{compact,vector}` → 退出 0，`dataset=rfc4-clean-news-20k`、`n_docs=20000`、
  `corpus_sha256=1480ab10cbefcb57bc1132871420b1f88d18ac5f260150080953f9441377a861`、
  快照 SHA `143724e3…4036`。
- 官方 8M 的 `offline.validate`（18 GB 快照 + 39 GB compact）没有在本轮重算，沿用
  既有接收证据 `official-offline-validate.json`（退出 0）的结论；
  本轮用启动身份校验（manifest/collection/维度/`count(exact=True)`）复核同一产物。

## 3. 真实服务（新闻 20k）

配置是仓库外临时目录中的私有 profile（允许绝对路径，含本机产物与模型位置，未进入 Git）：

| 字段 | 值 |
|---|---|
| `dataset` | `rfc4-clean-news-20k`（20000 篇，fingerprint `1480ab10…7a861`） |
| `index` | `…/artifacts/news20k/compact` |
| `vector` | `http://127.0.0.1:16433`（**专用转发代理** → 既有只读 Qdrant `16333`），`hnsw_ef=256`，`timeout_s=2.0` |
| `embedding` | `…/artifacts/model-local-official-20261102`，`BAAI/bge-small-zh-v1.5`@`7999e1d3…`，`cpu` |
| `retrieval` | `candidate_k=200`、`rrf_c=60.0` |
| `serve` | `127.0.0.1:18123`，`max_body_bytes=65536`，`request_timeout_s=10.0` |
| `policy` | `rule`（`rules-v1`） |

启动耗时约 **3.5 s** 到 `/health` 可用。启动 stderr 结构化日志含
`dataset/n_docs/host/port/边界/router_factory/policy_version` 与新增
`index_identity`/`vector_identity`/`search_config`/`policy`，回显
`collection=news_rfc4_clean20k`、`search={hnsw_ef:256, exact:false, timeout_s:2.0}`、
`embedding.revision=7999e1d33597…`、`bm25={k1:1.2,b:0.75,idf:log1p-lucene}`；
日志中没有查询文本、请求头或凭据（对服务日志检索查询子串结果为空，也没有 traceback）。

三阶段用**同一个服务进程**（PID 不变），后端由专用本地 TCP 转发代理模拟：

| 阶段 | 操作 | 结果 |
|---|---|---|
| healthy | 代理在线 | **23/23 PASS** |
| down | 停掉代理（同时关闭监听与已建立连接），共享 Qdrant 不动 | **10/10 PASS**：词法可用、dense/hybrid 503 `vector_unavailable`、adaptive 显式 `fallback=true` |
| recovered | 只重启代理，服务进程不重启 | **27/27 PASS** |

补充失败探针（同一进程）：把代理换成“接受连接但永不响应”的黑洞，`vector.timeout_s=2.0`
生效——dense 返回 **503，耗时 2.01 s**；adaptive 返回 200 且
`reason=vector_unavailable, fallback=true, executed_mode=lexical`；lexical 不受影响（0.02 s）。
这同时说明向量超时**不覆盖**查询编码耗时：dense 的 2 s 只是 Qdrant I/O 超时。

纯词法真实冒烟：`fixtures/tiny_index`（`dataset=tiny`，10 篇，无向量块）上
`--lexical-only` 验收 **9/9 PASS**，`index_identity.bm25` 回显产物声明参数，
`vector_identity=null`。

四模式评测（新闻，5 条冻结 dev 查询，`--fail-on-fallback`）：

| 模式 | HTTP 200 | fallback | 向量尝试/成功 | p50 / p95 |
|---|---|---|---|---|
| lexical | 5/5 | 0 | 0/0 | 171.9 / 315.4 ms |
| dense | 5/5 | 0 | 5/5 | 94.6 / 176.6 ms |
| hybrid | 5/5 | 0 | 5/5 | 123.5 / 297.2 ms |
| adaptive | 5/5 | 0 | 1/1 | 167.7 / 299.5 ms |

TREC run、`*.requests.json` 与逐请求事件、路由/延迟汇总都写在仓库外；qrels 只在评测器侧
使用，服务/日志/UI 都不读 qrels（探针与评测器均只走公开 HTTP）。

**身份错配提前拒绝（必要时连监听都不建立）**：把目标端口先用本进程占住，再启动四个错配
profile，四例都以退出码 2 结束且没有“无法监听”错误（说明未走到 bind）：

| 错配 | 观测 |
|---|---|
| `dataset` = 官方集合名对新闻索引 | 0.29 s，`dataset 不匹配` |
| 向量 manifest `corpus_sha256` 篡改 | 3.04 s，`collection 构建元数据与 manifest 不一致` |
| 向量 manifest `collection` 篡改 | 3.05 s，`无法读取 Qdrant collection: 404` |
| 向量 manifest `n_docs` 篡改 | 3.06 s，`collection 构建元数据与 manifest 不一致` |

## 4. 真实服务（官方 DuRetrieval 8,096,668）

护栏先行：确认旧官方 unit 均 `inactive`（`nlp-pr8-local-official-20261108-qdrant/-http`）、
`26333/26334` 空闲、`MemAvailable=30.95 GiB ≥ 30 GiB`、只启动本轮独有命名的 user
systemd unit，**不**恢复/覆盖/重建/写入 collection。

| 资源 | 设置 |
|---|---|
| Qdrant unit（`issue5-integration-official-qdrant`） | `MemoryHigh=16G`、`MemoryMax=20G`、`MemorySwapMax=0`、`CPUQuota=200%` |
| 应用 unit（`issue5-integration-official-http`） | `MemoryHigh=4G`、`MemoryMax=6G`、`MemorySwapMax=0`、`CPUQuota=200%` |

| 观测 | 值 |
|---|---|
| Qdrant `memory.peak` / `memory.max` | 1.41 GiB / 20 GiB，`swap.peak=0`，`oom_kill=0` |
| 应用 `memory.peak` / `memory.max` | 4.30 GiB / 6 GiB，`swap.peak=0`，`high` 事件 97,262 次、`max`/`oom_kill` 为 0 |
| 启动耗时 | 约 **105 s**（大 manifest/collection 身份核对） |
| 启动回显 | `dataset=dureader-official-dev-full-8096668`、`n_docs=8096668`、fingerprint `0ae8b117…061019`、`collection=real_bge_official8096668`、`search.hnsw_ef=256` |

资源口径说明：本轮 `Qdrant memory.peak` 只有 1.41 GiB，因为只跑了少量查询；既有接收证据
在某次更重负载下观测到约 16 GiB（大部分是 mmap 文件缓存）。两者都不是“整机只需这么多内存”
的结论，硬护栏是 `MemoryMax=20G/6G` 与 `MemorySwapMax=0`。

官方 profile 配置同样在仓库外临时目录，`vector.url` 指向**另一个专用转发代理**
（`26433` → 只读 `26333`），`timeout_s=30.0`，应用监听 `127.0.0.1:18127`。

| 阶段 | 结果 |
|---|---|
| healthy | **23/23 PASS** |
| down（停代理，Qdrant 不动） | **10/10 PASS** |
| recovered（只重启代理，应用与 Qdrant 都不重启） | **27/27 PASS** |

四模式评测（官方，5 条冻结 dev 查询，`--fail-on-fallback`）：四组均 5/5 HTTP 200、0 fallback；
dense 5/5 向量尝试，adaptive 5/5。p50 分别为 lexical 1.14 s、dense 0.34 s、hybrid 1.18 s、
adaptive 1.19 s（8M 语料上词法阶段是主项）。这只是 dev-only 接线冒烟，不是 808×4 正式结论。

结束后 `systemctl --user stop` 只停本轮两个 unit，`26333/26334/26433/18127` 与新闻侧
`18123/16433` 全部释放，无残留进程；既有只读服务 `127.0.0.1:18080`（review 应用）和
`127.0.0.1:16333`（review Qdrant）只被读取，验收结束后 `/health` 与 `/collections` 仍正常，
没有被停止、重启或写入。

## 5. 与既有证据的边界

- **本轮新执行**：新闻 `offline.validate`、两套真实服务的三阶段验收、身份错配拒绝、
  2 s 超时失败探针、两份 5 查询×4 模式评测、官方 profile 的受控启动/停止与 cgroup 观测、
  集成修正的回归测试与修正前代码的失败复现。
- **沿用既有证据（未重跑）**：官方 8M 的 `offline.validate` 退出 0；官方 collection 的
  完整 SHA/维度/Cosine/HNSW 恢复观测；新闻/官方冻结 ANN 对照；808×4 正式质量实验与
  150 查询新闻四臂重跑；接收机的 media SHA 验收。这些结论保持原状，本页不改写。
- 本页不含语料、查询、qrels、run 或机器私有证据；新闻/官方的 qrels 子集与全部原始日志
  都在仓库外临时目录，任务结束后可丢弃。

## 6. 已知限制

- 单进程 `ThreadingHTTPServer`；无认证、无 TLS、无速率限制、无并发上限、无优雅再平衡。
- 没有编码器硬 deadline：`request_timeout_s` 只是 socket I/O 超时，`vector.timeout_s` 只是
  Qdrant 客户端 I/O 超时，本地 BGE 前向耗时不受二者约束。
- `/health` 只是词法 liveness；`vector_ready` 只是启动时配置/身份对齐结论，不是后端实时健康。
- 后端失联时 dense/hybrid 直接 503、adaptive 显式回退；不做自动重试或多副本切换。
- 5 查询冒烟不构成质量、SLO、容量或 HNSW 性能结论；`search.exact=false` 也不证明某次查询
  实际走了 HNSW。
- 官方 profile 只验证了“既有恢复产物 + 只读启动”这一条路径；没有快照恢复、写入、重建、
  并发压测，也没有官方 test split 的盲测。

## 7. 集成修正（issue #5 收尾）

| 修正 | 内容 | 复现/验证 |
|---|---|---|
| A | `tests/run.py` 收录函数式 `tests/test_router_seam.py`（`unittest discover` 无法收集函数式用例）；unittest 类改由发现命令运行并写进部署文档 | `tests/run.py` 71 → **80/80 通过** |
| B | 非法 UTF-8 请求体由 `UnicodeDecodeError` 崩溃改为净化 400 + 关闭连接 + 结构化日志；`Content-Length` 只接受 ASCII 十进制，并在 `int()` 前按上限宽度定界（`²` 类数字与 5000 位十进制不再触发 `ValueError`） | 修正前：原始套接字收不到响应（连接被丢弃）/`ValueError`；修正后：400/413，且不进入检索、服务继续可用 |
| C | 验收探针接受 `index_identity.bm25=null`（旧/极简词法产物不补造参数），有值时仍逐字段校验；探针在 `-O`/`PYTHONOPTIMIZE` 下直接拒绝运行，而不是把失败静默判为 PASS | 修正前：`bm25=null` 判 FAIL、`-O` 下正常运行并给出 FAIL/PASS；修正后：单测覆盖两种取值，真实 `tiny` 词法服务 9/9 PASS |
| D | 启动结构化日志新增 `index_identity`/`vector_identity`/`search_config`/`policy`（与 `/index/info` 同源，来自 `engine.info()`；不含 query/凭据） | 新增单元断言启动记录四块内容；真实 news/官方启动日志实见上述字段 |
| E | 文档与实现对齐：省略 `hnsw_ef` 回显 `null`（不代填 256）、`embedding` 整块可省略、socket 超时 vs Qdrant I/O 超时（不覆盖编码）、私有配置允许绝对路径而模板仍只写相对路径、`$VAR`/`~` 按字面处理通常在启动加载时失败而非加载器无条件拒绝 | 文档/契约文档改动；真实启动与错配探针行为一致 |

修正后全量门禁（venv，最终 head 见交接报告）：`tests/run.py` **80/80**、
`unittest discover -s tests` **207 tests OK**（0 skip）、`unittest discover -s evaluation`
**91 tests OK**、`node --test tests/test_web.cjs` **13 pass**。

## 8. 终审跟进修正（P2，位于 `dac09aa` 之上的后续提交）

终审（只读静态复核）给出两条 P2，本轮按最小改动修掉并重新取数；没有新架构、没有新评审循环。

| 修正 | 内容 | 新增回归 |
|---|---|---|
| A | `SearchEngine.info()["config"]` 的 `policy_version` 改为回显**实际生效**策略版本（注入的自定义 Router 也一致），`config` 其余检索参数逐字段不变，冻结的 `RetrievalConfig` 不被改写 | `tests/test_router_seam.py::test_custom_policy_version_echoes_agree_and_config_stays_unchanged`：`info().config.policy_version`、`info().policy.policy_version`、`routing.policy_version` 三者一致，`candidate_k/rrf_c`/`search_config` 与冻结配置一致 |
| B | `scripts/online_acceptance.py` 的 `--lexical-only` 分支补跑八类请求校验矩阵（请求契约与向量后端无关）；矩阵**只**加到词法 profile，配置向量的 down 阶段仍是 10 项，healthy/recovered 仍是 23/27 项；不加词法分页假设 | `test_lexical_only_profile_expects_unavailable_vector_modes`（17 项、含 8 个矩阵检查、无 `pagination_lexical`）、`test_lexical_only_profile_fails_on_missing_request_validation`（假词法服务不校验请求时必须 FAIL）、`test_vector_down_phase_keeps_ten_checks_without_request_matrix` |

两条新回归都在修正前代码上先失败（移除 `_error_matrix` 调用后，词法分支只报 9 项且假服务
不校验请求时会“报绿”），修正后通过。

跟进修正后的复跑（同一分支，venv，私有端口，只读既有后端）：

| 项目 | 结果 |
|---|---|
| 真实 `fixtures/tiny_index` + `--lexical-only` | **17/17 PASS**（修正前 9 项 → 现 17 项，矩阵已在词法 profile 上执行） |
| 真实自定义 Router（`--router-factory` 指向仓库外 `module:factory`，`policy_version=probe-v2`）HTTP 回显 | `config.policy_version = policy.policy_version = routing.policy_version = probe-v2`，`candidate_k=200` 不变 |
| 真实 news 20k healthy（备用回环端口，自有转发代理 → 只读 `16333`） | **23/23 PASS**；`/index/info` 回显 `config.policy_version=rules-v1`、`policy.policy_version=rules-v1`、`candidate_k=200` |
| 同一 news 进程 down（停自有代理，后端不动） | **10/10 PASS**（计数未因矩阵改动而变化），代理恢复后 hybrid 即恢复（`vector_used=true`） |
| 门禁 | `tests/run.py` **81/81**、`unittest discover -s tests` **209 tests OK**、`unittest discover -s evaluation` **91 tests OK**、`node --test tests/test_web.cjs` **13 pass**、`pytest -q tests evaluation` **381 passed / 567 subtests** |

官方 8,096,668 profile **没有重跑**：两条修正一条只改 `/index/info` 的回显值，一条只作用于
`--lexical-only` 验收分支，都不改变默认 `rule` 策略的真实检索路径，也不改变启动身份校验。
第 1–7 节的官方证据（含 23/10/27 三阶段、cgroup 观测、四模式 dev 冒烟）仍然对应
`20f06f4` / `dac09aa` 的实际执行，明确保留、不改写、不冒充本轮重跑。

跟进期间自有进程（news 应用、两套转发代理、两套 tiny 词法服务）全部停止，端口释放；
既有只读服务 `127.0.0.1:18080` 与 `127.0.0.1:16333` 只被读取，结束后仍健康。

## 9. 合并前复核修正（2026-11-10，基于 `3bb1fd1`）

独立复核发现请求体 JSON 解析只捕获 `JSONDecodeError`：5000 位整数会触发普通
`ValueError`，深层嵌套会触发 `RecursionError`。两类请求都可小于 65536 字节上限，
原实现却会直接断连、输出线程 traceback，且缺失结构化 search 事件。

`online.server` 现将这两类解析异常统一送入既有的净化 HTTP 400 路径，不进入检索，
不回显查询或异常详情，并记录 `reason=invalid_request`。新增两条原始 HTTP 回归，
修复前均因空响应报错，修复后验证 400、结构化日志、无查询/traceback 泄漏，以及同一
服务继续处理 `/health` 和正常搜索。

固定 Python 3.10.14 环境的本轮门禁如下（`PY` 为复核 venv 的解释器）：

| 命令 | 结果 |
|---|---|
| `$PY -B -m unittest tests.test_observability -v` | 14 tests OK |
| `$PY -B tests/run.py` | 81/81 |
| `$PY -B -m unittest discover -s tests -p 'test_*.py'` | 211 tests OK |
| `$PY -B -m unittest discover -s evaluation -p 'test_*.py'` | 91 tests OK |
| `$PY -B -m pytest -q -p no:cacheprovider tests evaluation` | 383 passed / 567 subtests |
| `node --test tests/test_web.cjs` | 13 pass |
| `git diff --check` | 通过，无输出 |

另以独立子进程和临时回环端口启动真实 tiny profile：5000 位整数与 2000 层嵌套
均返回净化 400，日志含两条对应的 `invalid_request` 事件，且无查询或 traceback；
随后同一进程完成词法 HTTP 验收 17/17。仅本轮测试进程被停止，既有服务未被修改。

修正不改变正常请求、Router 或双索引检索路径；新闻 20k 与官方 8M 服务未在本轮重跑，
前述真实产物验收仍对应其原始提交。评测器尚不发送 `run_tag` 的跨 RFC 接线缺口保持
明确记录，不以本次请求解析修复冒充完成。
