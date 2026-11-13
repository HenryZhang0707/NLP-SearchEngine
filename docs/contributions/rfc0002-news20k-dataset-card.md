# RFC-0002 新华网 20k 数据卡

版本为 `clean-news/v1`，采集于 2026 年 11 月，固定 20,000 篇可检索文章和 200 条查询。该数据用于评估中文新闻检索，并为四模式端到端演示提供固定语料。现有 qrels 来源为 assistant 全文判定，独立人工评分与裁决待完成。 [RFC-0002](https://github.com/ShwStone/NLP-SearchEngine/issues/4) · [冻结输入与运行证据](../../evaluation/RESOURCES.md#团队自爬新闻测试集)

## 采集与文档

- 从新华网入口同域采集，深度上限 6，请求间隔至少 1 秒，响应上限 1,000,000 字节。遵守当时的 robots 规则，失败时拒绝继续抓取对应页面。清洗仅接受同域文章页，抽取标题和正文，移除指定模板节点，筛除无效页、乱码和过短正文。正文先按规范化 SHA-256 精确去重，再以 SimHash64 汉明距离 ≤3、正文长度比 ≥0.9 判定近重复。 [采集规则与复现命令](../../evaluation/README.md#cleaned-article-collection)
- 28,914 次采集尝试产生 28,927 次 HTTP 请求和 28,633 份原始响应捕获。20,000 次尝试接受为有效文档，5,666 次过滤，2,700 次精确重复，86 次近重复，另有 462 次非 HTML、体积超限、传输错误或中断。全部有效文档来自 `www.news.cn`，19,844 篇使用 `#detailContent`，156 篇使用备用 `#detail`；正文字数按 Unicode 码点统计，最少 80、中位 1,039、最多 170,588。
- 审计核对有序 `crawl-000001` 至 `crawl-020000`、语料及 sidecar 哈希、每篇接受文档的 sidecar 正文摘要、每条查询的来源文档身份。Sidecar 按尝试保存来源 URL、采集时间、抽取元数据和原始响应摘要。语料、原始缓存和可识别个体的文本留在 Git 外。公开可访问性与 robots 许可无法证明再分发权；这批材料的正文再分发权限及独立隐私复核仍待确认。维护步骤见下文。

采集期间曾从原始响应重算五篇样本的正文、标题及元数据，结果一致。另一个 150 篇采集中快照样本未发现符合当时阈值的残留近重复。两项抽样发生在最终冻结前。 [采集抽样记录](../../evaluation/RESOURCES.md)

## 查询与划分

候选查询来自 220 条预选来源，逐条检查所问内容是否由来源正文支撑。按原先冻结的来源顺序取前 200 条通过的查询，构建时未使用检索结果或 qrels。类型为解释 63、事实 50、事件 43、政策 35、比较 9。语料来自单一站点，且每条查询由一篇已有文章支撑，覆盖范围受新闻体裁、采集期及来源选择影响。显式布尔和短语语法由功能验收另行覆盖。

历史分配按 `SHA256(seed + NUL + query_id)` 升序取前 50 条作为 dev，其余 150 条作为 test。此次重放原规则并核对三个查询导出，另采用已有项目文本审计判据进行事后诊断。先做 NFKC、小写化并保留字母数字字符，再检查规范化精确相等以及字符 bigram 集合 Jaccard ≥0.8 的非精确匹配。单字符使用其自身作为唯一集合元素。

| 比较范围 | 查询对 | 规范化精确匹配 | 非精确、Jaccard ≥0.8 |
| --- | ---: | ---: | ---: |
| 新闻 dev × 新闻 test | 7,500 | 0 | 0 |
| 新闻全量 × RocketQA 完整 official-dev 查询 | 400,000 | 0 | 0 |

新闻 dev 和 test 的来源文档 ID 交集为 0。RocketQA 比较覆盖本地 train/dev/holdout 的 172/1,020/808 条查询，共 2,000 条，源集合为 official-dev。历史划分没有按近重复组分配；本次结果仅约束所用词面判据。另将 20,000 篇新闻正文与 DuReader 全部 8,096,668 篇 passage 的完整 `text` 字段逐字符匹配，两侧匹配文档数均为 0。该扫描同时复核官方语料 SHA-256 为 `bc67f3ccfe7423c039f04c7a5e7f6eae07a7f722087edf0caa7cfc694a8c45e8`。语义改写、文章片段与 passage 的子串重叠、经过编辑的跨站转载以及查询与全文的重叠超出本次诊断范围。

## 判定覆盖

由 lexical、dense、固定 hybrid、adaptive 和 lexical+RM3 各取 Top-20，再加入查询来源文档，按查询与文档的组合去重。盲化判定使用 0、1、2 三级，分别表示无实质相关性、有实质的部分答案或背景、直接提供有依据的答案。8,281 对包含 200 对身份匹配的 assistant 种子判定和 8,081 对 assistant 全文复核。未判文档保留未判状态，正式评测的 Recall 和 MAP 的相关文档分母来自池内已知正例。

| 划分 | 查询 | 已判对 | 等级 0/1/2 | 已判数/查询（最少/中位/最多） | 正例数/查询（最少/中位/最多） |
| --- | ---: | ---: | --- | --- | --- |
| dev | 50 | 2,067 | 1,903 / 86 / 78 | 25 / 42 / 50 | 1 / 1 / 16 |
| test | 150 | 6,214 | 5,700 / 274 / 240 | 26 / 41 / 54 | 1 / 2 / 28 |
| 合计 | 200 | 8,281 | 7,603 / 360 / 318 | 25 / 41 / 54 | 1 / 1 / 28 |

四组各 150 条正式 test 请求的 Top-10 均为 1,500/1,500 个位置有判定。Top-100 每组共 15,000 个位置，其中 lexical 已判 4,499 个（29.99%）、dense 4,215 个（28.10%）、hybrid 6,159 个（41.06%）、adaptive 4,812 个（32.08%）。Top-10 覆盖率受参评系统参与建池影响。复用同一语料评估新排序器时，应单独报告新 run 的判定覆盖。

独立人工评分沿用同一候选池。四位评分者覆盖全部 8,281 对，其中 1,656 对独立重叠。收齐评分并裁决后，通过[导入与复评命令](#人工评分复评)生成新 qrels，复算同一批四模式排名的质量、覆盖和区间。保留历史辅助判定的数据版本及结果。

## 人工评分复评

四份盲审包已准备并完成本地 SHA-256 回读。盲审包每行包含 `pair_id`、查询、标题和正文。协调者在取得内容访问授权后提供对应材料，并保留签收记录、原始映射和四人分配计划。评分者提交各自分配中的每一行，格式为 `{"pair_id":"...","relevance":0}`，等级只能取 0、1 或 2。将以下参数设置为冻结的完整盲审包、私有映射、计划及其预先记录的 SHA-256。

```bash
review_args=(
  --packet "$PACKET" --private-map "$PRIVATE_MAP"
  --plan "$PLAN" --plan-sha256 "$PLAN_SHA256"
  --reviewer reviewer-a "$GRADES_A" --reviewer reviewer-b "$GRADES_B"
  --reviewer reviewer-c "$GRADES_C" --reviewer reviewer-d "$GRADES_D"
)
python -m evaluation.annotation_intake "${review_args[@]}" \
  --prepare-adjudication disputes.jsonl adjudication-template.jsonl pre-adjudication.json
```

协调者在分发裁决材料前记录 `pre-adjudication.json` 的 SHA-256。评分分歧由独立裁决者复核。把模板复制到新文件，将各行的空等级填为 0、1 或 2。没有分歧时省去 `--adjudication`，仍传入准备收据及其已记录的摘要。首次构建盲审包可用 `python -m evaluation.annotation_packet --help` 查看输入契约。

```bash
python -m evaluation.annotation_intake "${review_args[@]}" \
  --pre-adjudication pre-adjudication.json "$TRUSTED_PRE_ADJUDICATION_SHA256" \
  --adjudication adjudicated.jsonl --qrels news-human.qrels.tsv --receipt intake.json
python -m evaluation.score_news_review "$NEWS_EVIDENCE_ROOT" \
  --qrels news-human.qrels.tsv --intake intake.json \
  --intake-sha256 "$TRUSTED_INTAKE_SHA256" --out news-human-test-summary.json
```

协调者在导入时记录 `intake.json` 的可信 SHA-256，评分阶段使用该记录验证输入身份。复评校验 8,281 对的精确覆盖与冻结的四份排名，报告 150 条留出查询的四项指标、Top-10/100 判定覆盖，以及 adaptive 减 hybrid 的配对区间（10,000 次，seed 20261028）。运行沿用原始 HTTP 排名。提交记录、私有映射、qrels 与单次运行输出留在 Git 外。

## 撤回和更新

1. 维护者在私下接收更正、撤回或敏感信息报告，以有权限读取的 sidecar 将来源定位到稳定文档 ID。若同时存在近重复或多次抓取，逐一确认影响，避免自动认定副本可替代源文档。未经确认的内容不纳入下一次演示或发布。
2. 用冻结证据预演影响。命令只读打开语料、查询、候选池和判定，验证哈希后输出 ID 和计数，不输出 URL、正文或标注依据。它报告源文档关联的查询、直接受影响的候选／判定、因撤回查询额外受影响的候选／判定，以及失去最后一个已判正例的幸存查询。

   ```bash
   python -B -m evaluation.news_withdrawal /path/to/rfc4-crawl-20261029 --doc-id crawl-000001
   ```

3. 获得数据所有者确认后，在单独的新版本中删除或更正对应文档；幸存文档保留外部 ID，幸存查询保留 dev/test 归属。重新构建词法和向量索引及快照，复核查询来源、重建候选池，并为变更文档或新候选重新标注。发布新的 SHA-256 清单、报告与运行结果，不能沿用旧版指标。只有经核对的新版本才可接替原部署。
4. 由持有副本的负责人核对部署、缓存、历史传输和接收方副本，按获准范围撤回或隔离旧版，再检查公开接口是否仍返回被撤回内容。预演成功只说明依赖可追踪，不表示正文已经删除、通过独立隐私审核或取得发布许可。

## 复验

从仓库根目录运行下列命令。输入为已获授权的冻结证据目录，沿用采集回执、查询冻结清单、官方查询划分、候选池和 qrels 清单、正式 test TREC runs 的原目录结构。

```bash
python -B -m evaluation.audit_crawled_dataset /path/to/rfc4-crawl-20261029
```

可附加 `--official-corpus /path/to/official-prepared-8096668/corpus.jsonl` 执行全文精确重叠扫描。该选项单次流式读取官方 JSONL，核对全量文档数和原始文件 SHA-256，并按解析后的完整 `text` 字段逐字符比较。

命令以只读方式打开输入，核对输入身份、覆盖和判定计数，只在标准输出打印聚合 JSON。此处重放使用 Python 3.13，Unicode 15.1.0。未完成独立人工盲审、许可复核或新闻主题覆盖的抽样判断。
