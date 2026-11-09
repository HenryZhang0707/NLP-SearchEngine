const form = document.querySelector("#search-form");
const queryInput = document.querySelector("#query");
const modeInput = document.querySelector("#mode");
const kInput = document.querySelector("#k");
const submitButton = document.querySelector("#submit");
const statusBox = document.querySelector("#status");
const errorBox = document.querySelector("#error");
const resultBox = document.querySelector("#results");
const observability = document.querySelector("#observability");
const summary = document.querySelector("#summary");
const routingBox = document.querySelector("#routing");
const timingsBox = document.querySelector("#timings");
const indexSummary = document.querySelector("#index-summary");
const indexDetails = document.querySelector("#index-identity");
const identityFactsBox = document.querySelector("#identity-facts");

const ROUTING_LABELS = {
  requested_mode: "请求模式",
  executed_mode: "执行模式",
  reason: "路由原因",
  score_kind: "分数类型",
  vector_used: "使用向量",
  vector_attempted: "尝试向量",
  fallback: "发生降级",
  candidate_k: "候选池",
  policy_version: "策略版本",
};

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function setStatus(message, kind = "") {
  statusBox.textContent = message;
  statusBox.className = `status ${kind}`.trim();
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

function hideError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
}

function clearObservability() {
  // A failed request must not leave the previous query's routing facts visible.
  summary.textContent = "";
  routingBox.innerHTML = "";
  timingsBox.innerHTML = "";
  observability.hidden = true;
}

function formatBoolean(value) {
  return value ? "是" : "否";
}

const MISSING_VALUE = "—";

function textField(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "number" && !Number.isFinite(value)) return "";
  return String(value).trim();
}

function firstField(...values) {
  for (const value of values) {
    const text = textField(value);
    if (text) return text;
  }
  return "";
}

function digestField(value) {
  const text = textField(value);
  if (!text) return null;
  return text.length > 16 ? `${text.slice(0, 12)}…` : text;
}

function joinParams(params) {
  if (!params || typeof params !== "object") return "";
  return Object.entries(params)
    .filter(([, value]) => textField(value) !== "")
    .map(([key, value]) => `${key}=${textField(value)}`)
    .join(" · ");
}

function vectorStatus(info) {
  // vector_ready means the operator configured an aligned vector backend at
  // startup; it is not a live health probe for the Qdrant service.
  const configured = (info.retrieval || {}).vector_ready;
  if (configured === true) {
    return { summary: "向量启动时已配置 · 非实时健康", detail: "启动时已配置（启动校验，非后端实时健康）", tone: "positive" };
  }
  if (configured === false) {
    return { summary: "仅词法模式", detail: "未配置（仅词法模式）", tone: "" };
  }
  return { summary: "向量状态未知", detail: "未提供", tone: "" };
}

function indexSummaryText(info) {
  const retrieval = info.retrieval || {};
  const index = retrieval.index_identity || {};
  const dataset = firstField(index.dataset, info.dataset) || "未知数据集";
  const nDocs = firstField(index.n_docs, info.n_docs) || "?";
  return `${dataset} · ${nDocs} 篇文档 · ${vectorStatus(info).summary}`;
}

function bm25Fields(bm25) {
  if (!bm25 || typeof bm25 !== "object") return "";
  const parts = [];
  if (textField(bm25.k1)) parts.push(`k1=${textField(bm25.k1)}`);
  if (textField(bm25.b)) parts.push(`b=${textField(bm25.b)}`);
  if (textField(bm25.idf)) parts.push(`idf=${textField(bm25.idf)}`);
  return parts.join(" · ");
}

function searchParamFields(search) {
  if (!search || typeof search !== "object") return "";
  if (search.exact === true && !textField(search.hnsw_ef)) return "exact=是（全量余弦扫描）";
  const parts = [];
  if (textField(search.hnsw_ef)) parts.push(`ef=${textField(search.hnsw_ef)}`);
  if (typeof search.exact === "boolean") parts.push(`exact=${search.exact ? "是" : "否"}`);
  if (textField(search.timeout_s)) parts.push(`timeout=${textField(search.timeout_s)} s`);
  return parts.join(" · ");
}

function retrievalParamFields(searchConfig, config) {
  const parts = [];
  const candidateK = firstField(searchConfig.candidate_k, config.candidate_k);
  const rrfC = firstField(searchConfig.rrf_c, config.rrf_c);
  if (candidateK) parts.push(`candidate_k=${candidateK}`);
  if (rrfC) parts.push(`RRF c=${rrfC}`);
  return parts.join(" · ");
}

function policyFields(policy, config) {
  const parts = [];
  const name = firstField(policy.name);
  const version = firstField(policy.policy_version, config.policy_version);
  if (name) parts.push(name);
  if (version) parts.push(`版本=${version}`);
  if (typeof policy.requires_statistics === "boolean") {
    parts.push(`需要统计=${policy.requires_statistics ? "是" : "否"}`);
  }
  return parts.join(" · ");
}

function identityFacts(info) {
  // /index/info keeps the lexical manifest at the top level and puts serving
  // identity under retrieval; every block is optional so each row degrades to
  // an explicit placeholder instead of "undefined".
  const retrieval = info.retrieval || {};
  const index = retrieval.index_identity || {};
  // Older /index/info versions only exposed the raw vector manifest; fall back
  // to it so the panel still shows the configured backend after an upgrade lag.
  const vector = retrieval.vector_identity || retrieval.vector_manifest || {};
  const searchConfig = retrieval.search_config || {};
  const config = retrieval.config || {};
  const policy = retrieval.policy || {};
  const analyzerInfo = info.analyzer || {};
  const embedding = vector.embedding || {};
  const bm25 = index.bm25 || info.bm25 || {};
  const status = vectorStatus(info);

  const facts = [];
  const add = (label, value, wide = false) => {
    const text = textField(value);
    facts.push({ label, value: text || MISSING_VALUE, wide });
  };
  const addDigest = (label, value) => {
    const digest = digestField(value);
    facts.push(digest
      ? { label, value: digest, copy: textField(value), wide: false }
      : { label, value: MISSING_VALUE, wide: false });
  };

  facts.push({ label: "向量能力", value: status.detail, tone: status.tone, wide: false });
  add("数据集", firstField(index.dataset, info.dataset));
  add("文档数", firstField(index.n_docs, info.n_docs));
  addDigest("语料指纹", firstField(index.corpus_sha256, vector.corpus_sha256));
  add("分词器", [textField(analyzerInfo.name), textField(analyzerInfo.version)].filter(Boolean).join(" "));
  addDigest("分词器哈希", firstField(index.analyzer_hash, analyzerInfo.config_hash));
  add("BM25 参数", bm25Fields(bm25), true);
  add("向量集合", vector.collection);
  add("向量后端", vector.backend);
  add("嵌入模型", embedding.model_id);
  addDigest("模型版本", embedding.revision);
  add("向量维度", embedding.dimension);
  add("查询前缀", embedding.query_prefix, true);
  add("构建参数", joinParams(vector.index_params), true);
  add("检索 HNSW", searchParamFields(vector.search), true);
  add("候选池 / RRF", retrievalParamFields(searchConfig, config), true);
  add("路由策略", policyFields(policy, config), true);
  return facts;
}

function renderIdentityFacts(info) {
  return identityFacts(info).map((fact) => {
    const className = fact.wide ? "fact wide" : "fact";
    const valueClass = fact.tone === "positive" ? "fact-value positive" : "fact-value";
    const title = fact.copy ? ` title="${escapeHtml(fact.copy)}"` : "";
    const copy = fact.copy
      ? `<button type="button" class="copy" data-copy="${escapeHtml(fact.copy)}" aria-label="${escapeHtml(`复制${fact.label}的完整值`)}">复制</button>`
      : "";
    return `<div class="${className}"><dt>${escapeHtml(fact.label)}</dt>`
      + `<dd class="${valueClass}"${title}><span class="fact-text">${escapeHtml(fact.value)}</span>${copy}</dd></div>`;
  }).join("");
}

async function copyIdentityValue(button) {
  const value = button.getAttribute("data-copy") || "";
  try {
    await navigator.clipboard.writeText(value);
    button.textContent = "已复制";
  } catch (_) {
    button.textContent = "复制失败";
  }
}

function renderRouting(routing) {
  routingBox.innerHTML = Object.entries(ROUTING_LABELS)
    .filter(([key]) => key in routing)
    .map(([key, label]) => {
      const value = typeof routing[key] === "boolean"
        ? formatBoolean(routing[key]) : routing[key];
      const className = routing[key] === true ? " fact-value positive" : " fact-value";
      return `<div class="fact"><dt>${escapeHtml(label)}</dt><dd class="${className}">${escapeHtml(value)}</dd></div>`;
    }).join("");
}

function renderTimings(timings) {
  const labels = { lexical: "词法", router: "路由", encode: "编码", vector: "向量", fusion: "融合" };
  timingsBox.innerHTML = Object.entries(labels).map(([key, label]) => {
    const value = Number(timings[key] || 0);
    return `<span class="timing"><b>${escapeHtml(label)}</b><span>${value.toFixed(3)} ms</span></span>`;
  }).join("");
}

function highlightedSnippet(text, highlights = []) {
  // API offsets count Unicode code points, not JavaScript UTF-16 code units.
  const source = Array.from(String(text || ""));
  const spans = highlights.map((highlight) => ({
    start: Math.max(0, Math.min(source.length, Number(highlight.start) || 0)),
    end: Math.max(0, Math.min(source.length, Number(highlight.end) || 0)),
    kind: highlight.kind === "expanded" ? "expanded" : "original",
  })).filter(({ start, end }) => end > start).sort((left, right) => left.start - right.start);

  const pieces = [];
  let cursor = 0;
  for (const span of spans) {
    if (span.start < cursor) {
      if (span.end <= cursor) continue;
      span.start = cursor;
    }
    if (span.start > cursor) pieces.push(escapeHtml(source.slice(cursor, span.start).join("")));
    pieces.push(`<mark class="${span.kind === "expanded" ? "expanded" : "original"}">${escapeHtml(source.slice(span.start, span.end).join(""))}</mark>`);
    cursor = span.end;
  }
  pieces.push(escapeHtml(source.slice(cursor).join("")));
  return pieces.join("") || "<span class=\"muted\">无摘要</span>";
}

function renderResults(data) {
  const results = Array.isArray(data.results) ? data.results : [];
  if (!results.length) {
    resultBox.innerHTML = `<div class="empty-state compact"><h2>没有找到结果</h2><p>可以尝试更换关键词或检索模式。</p></div>`;
    return;
  }
  resultBox.innerHTML = results.map((item) => `
    <article class="result-card">
      <div class="result-rank">${escapeHtml(item.rank)}</div>
      <div class="result-content">
        <div class="result-heading">
          <h2>${escapeHtml(item.title || item.doc_id)}</h2>
          <span class="score">${Number(item.score).toFixed(4)}</span>
        </div>
        <p class="doc-id">${escapeHtml(item.doc_id)}</p>
        <p class="snippet">${highlightedSnippet(item.snippet, item.highlights)}</p>
      </div>
    </article>`).join("");
}

function renderResponse(data) {
  const routing = data.routing || {};
  const count = Array.isArray(data.results) ? data.results.length : 0;
  summary.textContent = `${count} 条结果 · 总耗时 ${Number(data.took_ms || 0)} ms · ${data.api_version || "API"}`;
  renderRouting(routing);
  renderTimings(data.timings_ms || {});
  observability.hidden = false;
  renderResults(data);
  setStatus(`检索完成，返回 ${count} 条结果`, "success");
}

async function search(event) {
  event.preventDefault();
  const query = queryInput.value.trim();
  if (!query) {
    showError("请输入搜索内容");
    queryInput.focus();
    return;
  }
  const parsedK = Number.parseInt(kInput.value, 10);
  const k = Number.isInteger(parsedK) ? Math.max(1, Math.min(200, parsedK)) : 10;
  kInput.value = String(k);
  hideError();
  clearObservability();
  submitButton.disabled = true;
  setStatus("正在检索…");
  try {
    const response = await fetch("/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, mode: modeInput.value, k }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `请求失败（HTTP ${response.status}）`);
    renderResponse(data);
  } catch (error) {
    resultBox.innerHTML = "";
    clearObservability();
    setStatus("检索失败", "failure");
    showError(error instanceof Error ? error.message : "请求失败，请稍后重试");
  } finally {
    submitButton.disabled = false;
  }
}

async function loadIndexInfo() {
  try {
    const response = await fetch("/index/info");
    if (!response.ok) throw new Error("index info unavailable");
    const data = await response.json();
    indexSummary.textContent = indexSummaryText(data);
    identityFactsBox.innerHTML = renderIdentityFacts(data);
    indexDetails.hidden = false;
  } catch (_) {
    indexSummary.textContent = "索引信息暂不可用";
    identityFactsBox.innerHTML = "";
    indexDetails.hidden = true;
  }
}

form.addEventListener("submit", search);
identityFactsBox.addEventListener("click", (event) => {
  const target = event.target;
  const button = target && typeof target.closest === "function" ? target.closest("[data-copy]") : null;
  if (button) copyIdentityValue(button);
});
loadIndexInfo();
