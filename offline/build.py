"""索引构建（离线组）。

用法：
    python -m offline.build --corpus fixtures/tiny_corpus.jsonl \
        --out fixtures/tiny_index --dataset tiny

产物符合 contracts/index_format.py 的接口 A 约定。
验收：连跑两次 manifest 中各文件 sha256 一致（确定性构建）。
"""

import argparse
import json
import time
from pathlib import Path

from contracts import analyzer
from contracts.conformance import build_expected
from contracts.index_format import (
    DOCS, MANIFEST, OFFSETS, POSTINGS, STATS,
    BM25Spec, Manifest, sha256_of,
)


def build(corpus_path: Path, out_dir: Path, dataset: str,
          bm25: BM25Spec | None = None) -> Manifest:
    """构建 BM25 所需 tf/df/文档长度与位置索引，查询相关打分留在在线侧。"""
    with open(corpus_path, encoding="utf-8") as source:
        corpus = [json.loads(line) for line in source if line.strip()]
    ids = [row["doc_id"] for row in corpus]
    if not corpus or any(not isinstance(doc_id, str) or not doc_id for doc_id in ids):
        raise ValueError("语料不能为空，doc_id 必须为非空字符串")
    if len(ids) != len(set(ids)):
        raise ValueError("doc_id 必须唯一，不能构建无法对齐的双索引")
    exp = build_expected(corpus)

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / DOCS, "w", encoding="utf-8") as f:
        for docid in range(exp.n_docs):
            doc_id, title, text, _ = exp.docs[docid]
            f.write(json.dumps(
                {"docid": docid, "doc_id": doc_id, "title": title, "text": text},
                ensure_ascii=False) + "\n")
    with open(out_dir / POSTINGS, "w", encoding="utf-8") as f:
        for term in sorted(exp.postings):
            pl = exp.postings[term]
            f.write(json.dumps({"term": term, "df": len(pl), "docs": pl},
                               ensure_ascii=False) + "\n")
    with open(out_dir / OFFSETS, "w", encoding="utf-8") as f:
        for docid in range(exp.n_docs):
            spans = exp.docs[docid][3]
            f.write(json.dumps({"docid": docid, "spans": spans},
                               ensure_ascii=False) + "\n")
    with open(out_dir / STATS, "w", encoding="utf-8") as f:
        json.dump({"doc_lengths": {str(k): v for k, v in exp.doc_lengths.items()},
                   "avgdl": exp.avgdl}, f)

    files = {name: sha256_of(out_dir / name)
             for name in (DOCS, POSTINGS, OFFSETS, STATS)}
    manifest = Manifest(
        dataset=dataset,
        n_docs=exp.n_docs,
        avgdl=exp.avgdl,
        analyzer_name=analyzer.NAME,
        analyzer_version=analyzer.VERSION,
        analyzer_hash=analyzer.config_hash(),
        files=files,
        bm25=bm25 or BM25Spec(),
    )
    with open(out_dir / MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest.to_dict(), f, ensure_ascii=False, indent=2)
    return manifest


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="构建倒排索引")
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--k1", type=float, default=1.2)
    ap.add_argument("--b", type=float, default=0.75)
    args = ap.parse_args()

    t0 = time.perf_counter()
    m = build(args.corpus, args.out, args.dataset, BM25Spec(k1=args.k1, b=args.b))
    dt = time.perf_counter() - t0
    print(f"完成：{m.n_docs} 篇文档，avgdl={m.avgdl:.1f}，耗时 {dt:.2f}s -> {args.out}")
