#import "@preview/touying:0.6.1": *
#import themes.metropolis: *

// ---------- 配色 ----------
#let c-off = rgb("#2563eb") // 离线组·蓝
#let c-on  = rgb("#0d9488") // 在线组·青
#let c-ai  = rgb("#d97706") // AI 生成·橙
#let c-mut = rgb("#64748b") // 接缝·灰
#let tint(c) = c.lighten(88%)

#show: metropolis-theme.with(
  aspect-ratio: "16-9",
  footer: self => [NLP 课程大作业 · 开题报告],
  config-info(
    title: [基于 BM25 与向量检索的中文搜索引擎],
    subtitle: [自适应混合检索 · 开题报告],
    author: [石皓文 · 张恩瑞 · 许程 · 杜鹏宇],
    date: datetime.today(),
  ),
  config-common(preamble: {
    // MiSans（小米开源字体）：大陆规范字形；Noto 备用，region: "cn" 防日文异体
    set text(font: ("MiSans", "Noto Sans CJK SC"), lang: "zh", region: "cn")
  }),
)

#title-slide()

// ---------- 议程 ----------
== 报告提纲

#grid(
  columns: 3,
  gutter: 1.2em,
  ..(
    (c-off, [1], [词法检索\与语义检索]),
    (c-on, [2], [评测指标\与公开基线]),
    (c-ai, [3], [方案与\实验]),
  ).map(((c, n, t)) => rect(
    width: 100%, height: 5.5em, radius: 8pt,
    fill: tint(c), stroke: 1.5pt + c,
  )[
    #text(size: 42pt, fill: c, weight: "bold")[#n]
    #h(0.4em) #text(size: 20pt)[#t]
  ])
)

= 搜索引擎做了什么

== 一次查询的规模

#align(horizon + center)[
  #text(size: 32pt)[语料 #text(fill: c-off, weight: "bold")[100,001 篇] 文档，\
  BM25 单查目标 #text(fill: c-on, weight: "bold")[p95 < 100 ms]，\
  结果按相关度 #text(fill: c-ai, weight: "bold")[取前 10 篇]。]
]

== 词法检索：离线建索引，在线查索引

#align(horizon + center)[
  #image("figs/arch.svg", width: 96%)
]

== 核心数据结构是倒排索引

#align(horizon + center)[
  #image("figs/index.svg", width: 96%)
  #v(0.4em)
  #text(size: 17pt)[每个词项挂一张文档表；查询在命中词项的倒排表上直接打分]
]

== BM25：按词项匹配程度给文档打分

#grid(
  columns: (1.15fr, 1fr), gutter: 1.5em,
  [
    #set text(size: 19pt)
    #set par(spacing: 1em)
    #text(weight: "bold", fill: c-on)[输入：查询词 + 倒排表中的词频与文档长度]

    #text(weight: "bold")[IDF]：越罕见的词，区分文档的能力越强。

    #text(weight: "bold")[词频饱和]：重复加分，边际收益递减。

    #text(weight: "bold")[长度归一化]：抵消长文档靠词数堆出来的优势。
  ],
  [
    #rect(width: 100%, radius: 8pt, fill: tint(c-on), stroke: 1.5pt + c-on, inset: 16pt)[
      #text(size: 20pt, weight: "bold", fill: c-on)[词面零重合，BM25 就命中不了]
      #v(0.7em)
      #text(size: 17pt)[查询：手机掉水里怎么办\
      文档：设备进液后的应急措施]
      #v(0.7em)
      #text(size: 16pt)[查询与文档同义，共享词项却为零。型号、术语、专名恰好是它的强项。]
    ]
  ],
)
#v(0.6em)
#align(center)[#text(size: 16pt, fill: c-mut)[只访问命中词项的倒排表；核心打分与索引由我们自行实现]]

== 向量数据库：编码文本，再搜索相似向量

#align(center)[#image("figs/vector.svg", width: 100%)]
#v(0.5em)
#grid(
  columns: (1fr, 1.4fr), gutter: 1.2em,
  [
    #text(size: 18pt, weight: "bold", fill: c-off)[相似度：归一化向量的点积]\
    #text(size: 17pt)[$"sim"(q,d) = (q dot d) / (norm(q) norm(d))$]\
    #text(size: 15pt)[语义相近即得高分，词面可以完全不同。]
  ],
  [
    #text(size: 18pt, weight: "bold", fill: c-off)[HNSW：分层近邻图上的近似搜索]\
    #text(size: 15.5pt)[上层快速导航到邻近区域，下层扩大局部搜索；候选范围越大，召回越高，耗时同步上升。]
  ],
)
#v(0.4em)
#align(center)[#text(size: 14pt, fill: c-mut)[复用预训练中文编码器，检索层交付排序结果，答案生成留给上层应用；HNSW 为近似搜索，额外核对 Top-N 召回率]]

== BM25 与向量检索：优势互补，成本不同

#set text(size: 17pt)
#table(
  columns: (0.8fr, 1.5fr, 1.6fr),
  inset: 10pt, stroke: none,
  fill: (x, y) => if y == 0 { tint(c-mut) } else if calc.odd(y) { luma(247) },
  table.header([*比较维度*], [*BM25 · 词法检索*], [*向量检索 · 语义匹配*]),
  [匹配依据], [词频、IDF、文档长度], [编码器表示、向量相似度],
  [典型优势], [精确术语、型号、关键词], [同义表达、自然语言描述],
  [主要风险], [词面不重合时可能漏检], [语义接近但事实或型号不符],
  [离线成本], [构建倒排表与位置索引], [文档编码、向量存储、建近邻图],
  [在线成本], [分词、查表、打分], [查询编码、近邻搜索、结果返回],
)
#v(0.7em)
#align(center)[#text(size: 17pt)[两条路线各有失效场景，取舍落在同一语料、同一 qrels 上的实测数字]]

== SLM 查询扩写：为检索补充表达，不生成答案

#align(center)[#image("figs/slm.svg", width: 100%)]
#v(0.6em)
#grid(
  columns: 3, gutter: 1em,
  ..(
    (c-ai, [做什么], [用预训练小模型生成少量同义词或短改写；原查询保留，再组成第二阶段的检索输入。]),
    (c-off, [与向量检索的区别], [向量检索改变匹配空间；SLM 改变查询表达。二者可以串联，也可以跳过扩写直接检索。]),
    (c-on, [质量与成本约束], [保留实体和否定，显式约束不改写；限制生成长度，超时回退原查询，扩写收益需实测。]),
  ).map(((c, title, body)) => rect(
    width: 100%, height: 7.3em, radius: 8pt,
    fill: tint(c), stroke: 1.5pt + c, inset: 12pt,
  )[
    #set align(top)
    #text(size: 18pt, weight: "bold", fill: c)[#title]
    #v(0.5em)
    #text(size: 15.5pt)[#body]
  ])
)
#v(0.6em)
#align(center)[#text(size: 14pt, fill: c-mut)[SLM 是可选查询预处理，不是新的排序器；生成可能引入语义漂移，需同时测量质量与耗时]]

== 三种查询能力

#grid(
  columns: 3,
  gutter: 1em,
  rect(width: 100%, height: 7em, radius: 8pt, fill: tint(c-off), stroke: 1.5pt + c-off)[
    #text(weight: "bold", fill: c-off, size: 20pt)[布尔检索]
    #v(0.5em)
    #text(font: "Noto Sans Mono CJK SC", size: 13pt)[感冒 AND 发烧\
    NOT 流感]
  ],
  rect(width: 100%, height: 7em, radius: 8pt, fill: tint(c-off), stroke: 1.5pt + c-off)[
    #text(weight: "bold", fill: c-off, size: 20pt)[短语检索]
    #v(0.5em)
    #text(font: "Noto Sans Mono CJK SC", size: 13pt)["心肌梗死的症状"]\
    #text(size: 14pt)[按连续位置匹配]
  ],
  rect(width: 100%, height: 7em, radius: 8pt, fill: tint(c-ai), stroke: 2.5pt + c-ai)[
    #text(weight: "bold", fill: c-ai, size: 20pt)[语义检索]　#text(size: 13pt, fill: c-ai)[★ 混合检索]
    #v(0.5em)
    #text(size: 14pt)[手机掉水里怎么办\
    #sym.arrow.r.double 设备进液后的应急措施]
  ],
)
#v(1em)
#align(center)[
  #text(size: 16pt)[
    自然语言查询默认先走 BM25，再按路由信号补语义\
    显式 AND / NOT / 引号作为硬约束，在词法层过滤完再进入后续阶段
  ]
]

= 我们用公开数据评测检索效果

== 三个指标分别衡量排序、召回和速度

#grid(
  columns: (1.1fr, 1fr),
  gutter: 1.2em,
  [
    #align(horizon + center)[
      #image("figs/metrics.svg", width: 92%)
      #v(0.2em)
      #text(size: 15pt)[两种排序命中同样 3 篇相关文档（#text(fill: c-on)[●]）；越靠前，DCG 越高]
    ]
  ],
  [
    #align(horizon)[
    #set par(leading: 0.9em)
    #text(size: 20pt, weight: "bold")[nDCG\@10]\
    #text(size: 12pt)[$"DCG" = sum_(i=1)^10 "rel"_i slash log_2 (i+1)$，再除以理想排序的 DCG\@10]\
    #text(size: 15.5pt, fill: c-mut)[rel 取 0/1，衡量前 10 条的排序质量]
    #v(0.65em)
    #text(size: 20pt, weight: "bold")[Recall\@10]\
    #text(size: 12pt)[前 10 条覆盖的相关文档数 $slash$ 相关文档总数]\
    #text(size: 15.5pt, fill: c-mut)[衡量召回，是否漏掉相关文档]
    #v(0.65em)
    #text(size: 20pt, weight: "bold")[延迟 p95]\
    #text(size: 12pt)[95% 的查询快于此值；另报 p50 与索引加载耗时]\
    #text(size: 15.5pt, fill: c-mut)[衡量线上响应速度]
    ]
  ],
)

== 所有数字都出自 DuRetrieval

#align(horizon + center)[
#grid(
  columns: 3,
  gutter: 1em,
  ..(
    ([100,001], [网页段落]),
    ([2,000], [查询]),
    ([9,839], [人工相关性标注]),
  ).map(((n, t)) => rect(
    width: 100%, height: 5.2em, radius: 8pt,
    fill: tint(c-mut), stroke: 1pt + luma(180),
  )[
    #text(size: 38pt, weight: "bold", fill: c-off)[#n]\
    #text(size: 17pt)[#t]
  ])
)
#v(1em)
#text(size: 18pt)[
  数据集：#text(weight: "bold")[DuRetrieval]，取自 C-MTEB 中文检索基准\
  基线核对：分词、qrels 与 k1/b 统一，自实现 BM25 对 #text(weight: "bold")[bm25s] 基线，nDCG\@10 差距 < 2%\
  数据统计：平均文档 331.32 字符、平均查询 9.29 字符、每查询 4.92 篇相关文档
]
]

= 方案与实验设计

== 自适应混合检索：按需语义补充，可选 SLM 扩写

#align(horizon + center)[
  #text(size: 18pt)[#text(weight: "bold")[研究问题]：能否按需调用向量检索与 SLM 扩写，取得更好的质量—成本折中？]
  #v(0.6em)
  #image("figs/router.svg", width: 100%)
  #v(0.5em)
  #text(size: 15.5pt)[
    #text(weight: "bold")[三条路径]：直接 BM25 / 原查询向量补充 / SLM 扩写后向量补充\
    #text(weight: "bold")[保守边界]：显式约束走词法；SLM 超时退回原查询；原始 BM25 排名始终保留
  ]
  #v(0.4em)
  #text(size: 14pt, fill: c-mut)[查询与首轮结果特征结合调用预算；阈值在开发集冻结；SLM 仅做检索预处理，不替代轻量 Router]
]

== RRF：按名次融合两路结果

#grid(
  columns: (1fr, 1.1fr), gutter: 1.5em,
  [
    #rect(width: 100%, radius: 8pt, fill: tint(c-on), stroke: 1.5pt + c-on, inset: 16pt)[
      #text(size: 20pt, weight: "bold", fill: c-on)[RRF · 倒数排名融合]
      #v(0.5em)
      #text(size: 20pt)[$"RRF"(d) = sum_(s in {B,V}) 1 / (c + "rank"_s(d))$]
      #v(0.5em)
      #text(size: 16pt)[B / V 分别是 BM25 / 向量排名；未入某路候选贡献为 0，c 在开发集固定。\
      按内部 docid 合并、去重、排序，同分按 docid 升序。]
    ]
  ],
  [
    #set text(size: 17pt)
    #set par(spacing: 0.8em)
    #text(weight: "bold")[RRF 只看名次]\
    BM25 与余弦相似度的量纲、分布随语料漂移；名次是两路共通的尺度，RRF 直接在其上融合。

    #text(weight: "bold")[路由省下的开销]\
    直接返回时跳过 SLM、查询编码、近邻搜索与融合；进入语义分支也可跳过 SLM，离线成本照常。

    #text(weight: "bold")[自建与复用]\
    词法核心手写，复用预训练编码器、SLM 与向量库；SLM 只改写语义分支输入，不替换首轮候选。
  ],
)

== 两个接口划出两组的分工

#align(horizon + center)[
  #image("figs/split.svg", width: 100%)
  #v(0.3em)
  #text(size: 15pt)[接口 A 扩展为索引产物与版本约定：倒排索引 + 向量库快照，共用 doc_id。\
  接口 B 仍为 HTTP；评测器独立调用；#text(weight: "bold")[UI 与评测器由 AI 生成、人工验收]]
]

== 双索引与 Router 契约已落为代码骨架

#grid(
  columns: (1.25fr, 1fr),
  gutter: 1.5em,
  [
    #rect(width: 100%, radius: 8pt, fill: rgb("#23373b"), inset: 14pt)[
      #set text(font: "Noto Sans Mono CJK SC", size: 12pt)
      #set par(leading: 0.6em)
      #let ind = h(1.1em)
      #text(fill: rgb("#7dd3fc"))[contracts/]  #text(fill: rgb("#94a3b8"))[双方共有]\
      #ind #text(fill: rgb("#e2e8f0"))[analyzer.py     唯一分词器+哈希]\
      #ind #text(fill: rgb("#e2e8f0"))[index_format.py 接口A·格式校验]\
      #ind #text(fill: rgb("#e2e8f0"))[reader.py       IndexReader 协议]\
      #ind #text(fill: rgb("#e2e8f0"))[retrieval.py    向量/编码/路由配置]\
      #ind #text(fill: rgb("#e2e8f0"))[api.py          接口B·四模式+Schema]\
      #ind #text(fill: rgb("#e2e8f0"))[conformance.py  同一套验收测试]\
      #text(fill: rgb("#7dd3fc"))[offline/]   #text(fill: rgb("#94a3b8"))[离线组]\
      #ind #text(fill: rgb("#e2e8f0"))[build · build_hybrid · vector_build]\
      #text(fill: rgb("#7dd3fc"))[online/]    #text(fill: rgb("#94a3b8"))[在线组]\
      #ind #text(fill: rgb("#e2e8f0"))[memory_index · parser · scorers · highlight]\
      #ind #text(fill: rgb("#e2e8f0"))[router · fusion · vector_index · engine · server]\
      #text(fill: rgb("#7dd3fc"))[fixtures/ tests/] #text(fill: rgb("#94a3b8"))[34 项契约测试通过]
    ]
  ],
  [
    #align(horizon)[
    #set text(size: 16pt)
    #set par(leading: 0.65em, spacing: 1.7em)
    #text(weight: "bold")[HTTP 四种 mode 已固定]\
    lexical / dense / hybrid / adaptive；响应回显 routing 与 timings_ms

    #text(weight: "bold")[tiny 索引已跑通]\
    #box(text(font: "Noto Sans Mono CJK SC", size: 14pt)[build → load → /search]) 全链路跑通，\
    双路假数据对齐

    #text(weight: "bold")[新增：双索引一致性与 Router 测试]\
    校验语料指纹、ID、编码器与向量维度；\
    真实模型 / Qdrant 与 SLM 扩写待接入
    ]
  ],
)

== 并行开发先冻结词法与向量两套契约

#align(horizon + center)[
  #image("figs/parallel.svg", width: 86%)
  #v(0.1em)
  #text(size: 14.5pt)[共享 doc_id 与语料版本，向量索引仅使用同一批文档；冻结编码器版本、维度与截断规则\
  联调单列 BM25、SLM 扩写、查询编码、向量检索与融合耗时；SLM 缓存命中 / 未命中分别计时]
]

== 四组主实验 + SLM 查询扩写消融

#align(horizon)[
#table(
  columns: (1.8fr, 1fr, 1fr, 1fr, 1.4fr),
  align: center + horizon,
  stroke: none,
  inset: 10pt,
  table.hline(stroke: 1.5pt),
  table.header(
    [*检索方式*], [*nDCG\@10*], [*Recall\@10*], [*延迟*], [*定位*],
  ),
  table.hline(stroke: 0.8pt),
  [BM25], [待测], [待测], [待测], [词法基线],
  [纯向量检索], [待测], [待测], [待测], [语义基线],
  [固定混合 + RRF], [待测], [待测], [待测], [每次走两路],
  [自适应混合 + RRF], [待测], [待测], [待测], [按需走向量 ★],
  [自适应 + SLM 扩写], [待测], [待测], [待测], [预处理增益],
  table.hline(stroke: 1.5pt),
)
#v(0.6em)
#align(center)[
  #text(size: 15pt)[前四行分离检索 / 路由收益；末行固定同一路由、候选数与 RRF 参数，仅开关 SLM 预处理\
  同一语料、查询划分与 qrels；生成配置冻结，报告质量差值与 paired bootstrap 置信区间]
]
]

== 路由评测：检索质量与在线成本一起看

#grid(
  columns: 3,
  gutter: 1em,
  ..(
    (c-off, [1 · 开发集定规则], [开发集比较 BM25 与固定混合；确定路由特征和阈值后冻结，测试集只做验证。]),
    (c-on, [2 · 测试集看质量], [冻结规则后在独立测试集比较 nDCG\@10、Recall\@10，报告相对固定混合的质量变化。]),
    (c-ai, [3 · 实测成本变化], [记录平均延迟、p50/p95、向量与 SLM 触发率；SLM 耗时单列，区分缓存命中与未命中。]),
  ).map(((c, title, body)) => rect(
    width: 100%, height: 8.1em, radius: 8pt,
    fill: tint(c), stroke: 1.5pt + c, inset: 12pt,
  )[
    #set align(top)
    #text(size: 18pt, weight: "bold", fill: c)[#title]
    #v(0.6em)
    #text(size: 15.5pt)[#body]
  ])
)
#v(0.8em)
#align(center)[
  #text(size: 16pt)[
    改变路由阈值，展示 #text(weight: "bold")[检索质量—分支触发率—平均延迟] 的权衡曲线\
    向量与 SLM 慢分支可能主导 p95；计入扩写开销，不把缓存加速误当算法收益
  ]
]

== 里程碑按依赖顺序推进

#align(horizon + center)[
  #image("figs/milestones.svg", width: 94%)
  #v(0.4em)
  #text(size: 16pt)[顺序约束：#text(weight: "bold")[带词位置的倒排索引在 E1 前定稿]\
  在线组的短语过滤与高亮按定稿格式开发，E1 之后双方各自推进\
  #text(fill: c-ai)[E2：词法 / 向量 / 固定混合基线冻结 → E3：自适应路由、SLM 消融与演示]]
]

#focus-slide[
  #set align(center)
  谢谢\
  请各位老师批评指正
]
