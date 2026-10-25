"use strict";

// Optional browser-logic checks: node --test tests/test_web.cjs (Node.js 18+).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const context = vm.createContext({
  document: { querySelector: () => ({ addEventListener() {} }) },
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
