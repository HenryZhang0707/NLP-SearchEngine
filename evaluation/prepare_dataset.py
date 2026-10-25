"""Validate and deterministically split an external retrieval dataset.

The project deliberately does not vendor DuRetrieval or model weights.  This
small standard-library command turns a downloaded JSONL/TREC release into the
stable files consumed by :mod:`evaluation.evaluate`, while recording hashes,
counts, and the exact split policy.  It accepts the common corpus/query field
names used by C-MTEB style exports and fails early on duplicate IDs or qrels
that point outside the corpus.

Example::

    python3 -m evaluation.prepare_dataset \
      --corpus data/raw/corpus.jsonl --queries data/raw/queries.jsonl \
      --qrels data/raw/qrels.tsv --out data/duretrieval \
      --expected-docs 100001 --expected-queries 2000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Sequence

from .evaluate import EvaluationError


SPLITS = ("train", "dev", "test")


def _text(value: object, label: str, line_no: int, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationError(f"{path}:{line_no}: {label} 必须是非空字符串")
    return value


def _record_id(value: object, label: str, line_no: int, path: Path) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise EvaluationError(f"{path}:{line_no}: {label} 必须是字符串或整数")
    result = str(value).strip()
    if not result:
        raise EvaluationError(f"{path}:{line_no}: {label} 不能为空")
    return result


def _load_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise EvaluationError(f"无法读取 {path}: {error}") from error
    records: list[dict] = []
    for line_no, raw in enumerate(lines, 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise EvaluationError(f"{path}:{line_no}: JSON 格式错误") from error
        if not isinstance(value, dict):
            raise EvaluationError(f"{path}:{line_no}: JSONL 记录必须是对象")
        records.append(value)
    if not records:
        raise EvaluationError(f"输入为空: {path}")
    return records


def load_corpus(path: str | Path) -> list[dict[str, str]]:
    """Load corpus records and normalize ``id/title/text`` fields."""

    path = Path(path)
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for line_no, value in enumerate(_load_jsonl(path), 1):
        raw_id = value.get("doc_id", value.get("id", value.get("_id")))
        doc_id = _record_id(raw_id, "doc_id", line_no, path)
        if doc_id in seen:
            raise EvaluationError(f"{path}:{line_no}: doc_id 重复: {doc_id!r}")
        seen.add(doc_id)
        title = value.get("title", "")
        if not isinstance(title, str):
            raise EvaluationError(f"{path}:{line_no}: title 必须是字符串")
        text = value.get("text", value.get("contents", value.get("content")))
        result.append({"doc_id": doc_id, "title": title, "text": _text(text, "text", line_no, path)})
    return result


def load_query_records(path: str | Path) -> list[tuple[str, str]]:
    """Load JSONL queries using ``qid/query`` or ``id/text`` field names."""

    path = Path(path)
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line_no, value in enumerate(_load_jsonl(path), 1):
        raw_id = value.get("qid", value.get("query_id", value.get("id", value.get("_id"))))
        qid = _record_id(raw_id, "qid", line_no, path)
        if qid in seen:
            raise EvaluationError(f"{path}:{line_no}: qid 重复: {qid!r}")
        query = value.get("query", value.get("text"))
        query = _text(query, "query", line_no, path)
        seen.add(qid)
        result.append((qid, query))
    return result


def _qrel_rows(path: Path) -> list[tuple[str, str, float]]:
    """Read qrels and keep a canonical qid/docid/relevance representation."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise EvaluationError(f"无法读取 {path}: {error}") from error
    rows: list[tuple[str, str, float]] = []
    seen: dict[tuple[str, str], float] = {}
    for line_no, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) >= 4:
            qid, docid, relevance_raw = fields[0], fields[2], fields[3]
        elif len(fields) == 3:
            qid, docid, relevance_raw = fields
        else:
            raise EvaluationError(f"{path}:{line_no}: qrels 至少需要 3 列")
        qid = _record_id(qid, "qid", line_no, path)
        docid = _record_id(docid, "docid", line_no, path)
        try:
            relevance = float(relevance_raw)
        except ValueError as error:
            raise EvaluationError(f"{path}:{line_no}: relevance 不是数字") from error
        if relevance < 0 or relevance != relevance or relevance in (float("inf"), float("-inf")):
            raise EvaluationError(f"{path}:{line_no}: relevance 必须是非负有限数字")
        key = (qid, docid)
        previous = seen.get(key)
        if previous is not None and previous != relevance:
            raise EvaluationError(f"{path}:{line_no}: qrel 重复且 relevance 不一致")
        if previous is None:
            seen[key] = relevance
            rows.append((qid, docid, relevance))
    if not rows:
        raise EvaluationError(f"qrels 为空: {path}")
    return rows


def _split_for(qid: str, seed: int, dev_fraction: float, test_fraction: float) -> str:
    digest = hashlib.sha256(f"{seed}\0{qid}".encode("utf-8")).digest()
    point = int.from_bytes(digest[:8], "big") / float(1 << 64)
    if point < dev_fraction:
        return "dev"
    if point < dev_fraction + test_fraction:
        return "test"
    return "train"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_dataset(corpus_path: str | Path, queries_path: str | Path,
                    qrels_path: str | Path, out_dir: str | Path, *, seed: int = 2026,
                    dev_fraction: float = 0.1, test_fraction: float = 0.8,
                    expected_docs: int | None = None,
                    expected_queries: int | None = None) -> dict:
    """Normalize inputs, write stable split files, and return the manifest."""

    if type(seed) is not int:
        raise EvaluationError("seed 必须是整数")
    if not 0 <= dev_fraction < 1 or not 0 <= test_fraction < 1 or dev_fraction + test_fraction >= 1:
        raise EvaluationError("dev/test 比例必须非负且总和小于 1")
    corpus = load_corpus(corpus_path)
    queries = load_query_records(queries_path)
    qrel_rows = _qrel_rows(Path(qrels_path))
    if expected_docs is not None and len(corpus) != expected_docs:
        raise EvaluationError(f"文档数不符: {len(corpus)} != {expected_docs}")
    if expected_queries is not None and len(queries) != expected_queries:
        raise EvaluationError(f"查询数不符: {len(queries)} != {expected_queries}")
    doc_ids = {record["doc_id"] for record in corpus}
    query_ids = {qid for qid, _ in queries}
    unknown_qids = sorted({qid for qid, _, _ in qrel_rows} - query_ids)
    unknown_docs = sorted({docid for _, docid, _ in qrel_rows} - doc_ids)
    if unknown_qids:
        raise EvaluationError(f"qrels 含未知 qid: {unknown_qids[:3]}")
    if unknown_docs:
        raise EvaluationError(f"qrels 含未知 doc_id: {unknown_docs[:3]}")
    out = Path(out_dir)
    if out.exists() and not out.is_dir():
        raise EvaluationError(f"输出路径不是目录: {out}")
    if out.exists() and any(out.iterdir()):
        raise EvaluationError(f"输出目录必须为空，以免混入旧数据: {out}")
    out.mkdir(parents=True, exist_ok=True)
    corpus_out = out / "corpus.jsonl"
    with corpus_out.open("w", encoding="utf-8") as handle:
        for record in corpus:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    assignments = {qid: _split_for(qid, seed, dev_fraction, test_fraction)
                   for qid, _ in queries}
    for split in SPLITS:
        with (out / f"queries.{split}.tsv").open("w", encoding="utf-8") as handle:
            for qid, query in queries:
                if assignments[qid] == split:
                    handle.write(f"{qid}\t{query}\n")
        with (out / f"qrels.{split}.tsv").open("w", encoding="utf-8") as handle:
            for qid, docid, relevance in qrel_rows:
                if assignments[qid] == split:
                    handle.write(f"{qid} 0 {docid} {relevance:g}\n")
    manifest = {
        "schema_version": 1,
        "seed": seed,
        "split_fractions": {"dev": dev_fraction, "test": test_fraction,
                             "train": 1.0 - dev_fraction - test_fraction},
        "counts": {"documents": len(corpus), "queries": len(queries),
                    "qrels": len(qrel_rows)},
        "splits": {split: sum(value == split for value in assignments.values())
                   for split in SPLITS},
        "files": {},
    }
    for path in sorted(out.iterdir()):
        if path.is_file() and path.name != "dataset_manifest.json":
            manifest["files"][path.name] = {"sha256": _sha256(path), "bytes": path.stat().st_size}
    manifest_path = out / "dataset_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校验并固定外部检索数据集的输入与划分")
    parser.add_argument("--corpus", required=True, type=Path, help="JSONL corpus")
    parser.add_argument("--queries", required=True, type=Path, help="JSONL queries")
    parser.add_argument("--qrels", required=True, type=Path, help="TREC qrels")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--dev-fraction", type=float, default=0.1)
    parser.add_argument("--test-fraction", type=float, default=0.8)
    parser.add_argument("--expected-docs", type=int)
    parser.add_argument("--expected-queries", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = prepare_dataset(
            args.corpus, args.queries, args.qrels, args.out, seed=args.seed,
            dev_fraction=args.dev_fraction, test_fraction=args.test_fraction,
            expected_docs=args.expected_docs, expected_queries=args.expected_queries)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    except (EvaluationError, OSError) as error:
        print(f"dataset preparation error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
