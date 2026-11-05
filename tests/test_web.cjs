"use strict";

// Optional browser-logic checks: node --test tests/test_web.cjs (Node.js 18+).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const nodes = Object.fromEntries([
  "#search-form", "#query", "#mode", "#k", "#submit", "#status", "#error",
  "#results", "#observability", "#summary", "#routing", "#timings", "#index-summary",
].map((selector) => [selector, {
  addEventListener() {}, innerHTML: "", textContent: "", hidden: false, value: "",
}]));
const context = vm.createContext({
  document: { querySelector: (selector) => nodes[selector] },
  fetch: async () => { throw new Error("No network in UI tests"); },
});
vm.runInContext(
  fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8"),
  context,
  { filename: "web/app.js" },
);
const { highlightedSnippet } = context;

function highlight(start, end, kind = "original") {
  return { start, end, kind };
}

test("BMP highlights retain their character offsets", () => {
  assert.equal(
    highlightedSnippet("心肌梗死症状", [highlight(0, 4)]),
    '<mark class="original">心肌梗死</mark>症状',
  );
});

test("non-BMP characters before a hit do not shift its highlight", () => {
  assert.equal(
    highlightedSnippet("😀 apple", [highlight(2, 7)]),
    '😀 <mark class="original">apple</mark>',
  );
});

test("highlight boundaries never split surrogate pairs", () => {
  assert.equal(
    highlightedSnippet("𠮷野家", [highlight(0, 1, "expanded")]),
    '<mark class="expanded">𠮷</mark>野家',
  );
});

test("highlight clipping uses the snippet's code-point length", () => {
  assert.equal(highlightedSnippet("😀 apple", [highlight(7, 20)]), "😀 apple");
  assert.equal(
    highlightedSnippet("😀 apple", [highlight(-5, 20)]),
    '<mark class="original">😀 apple</mark>',
  );
});

test("Unicode handling preserves HTML escaping and empty snippets", () => {
  assert.equal(
    highlightedSnippet("<script>&😀", [highlight(0, 8)]),
    '<mark class="original">&lt;script&gt;</mark>&amp;😀',
  );
  assert.equal(highlightedSnippet(""), '<span class="muted">无摘要</span>');
});

async function successfulSearch() {
  nodes["#query"].value = "检索";
  nodes["#mode"].value = "hybrid";
  nodes["#k"].value = "10";
  context.fetch = async () => ({
    ok: true,
    json: async () => ({
      api_version: "2.0", took_ms: 2, routing: { executed_mode: "hybrid" },
      timings_ms: { vector: 1 },
      results: [{ rank: 1, doc_id: "previous-doc", title: "旧结果", score: 1,
        snippet: "检索", highlights: [{ start: 0, end: 2 }] }],
    }),
  });
  await context.search({ preventDefault() {} });
  assert.equal(nodes["#observability"].hidden, false);
  assert.match(nodes["#routing"].innerHTML, /hybrid/);
  assert.match(nodes["#results"].innerHTML, /previous-doc/);
}

function assertFailedSearchIsCleared() {
  assert.equal(nodes["#summary"].textContent, "");
  assert.equal(nodes["#routing"].innerHTML, "");
  assert.equal(nodes["#timings"].innerHTML, "");
  assert.equal(nodes["#observability"].hidden, true);
  assert.equal(nodes["#results"].innerHTML, "");
  assert.equal(nodes["#submit"].disabled, false);
  assert.equal(nodes["#error"].hidden, false);
}

test("HTTP failures clear the previous successful search facts", async () => {
  await successfulSearch();
  context.fetch = async () => ({
    ok: false, status: 503, json: async () => ({ error: "vector unavailable" }),
  });
  await context.search({ preventDefault() {} });
  assertFailedSearchIsCleared();
  assert.equal(nodes["#error"].textContent, "vector unavailable");
});

test("render failures clear partially rendered routing and timing facts", async () => {
  await successfulSearch();
  context.fetch = async () => ({
    ok: true,
    json: async () => ({ routing: { executed_mode: "dense" }, timings_ms: { vector: 99 },
      results: [{ rank: 1, doc_id: "bad-result", snippet: "检索", highlights: {} }] }),
  });
  await context.search({ preventDefault() {} });
  assertFailedSearchIsCleared();
});
