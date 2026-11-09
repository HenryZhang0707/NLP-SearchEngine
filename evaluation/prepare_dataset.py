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
      --qrels data/raw/qrels.tsv --out data/duretrieval

The full normalizer loads the corpus into memory. For resources already
validated against their corpus, use ``--queries-only`` to split only queries
and qrels, retaining the same seed/hash assignment. JSONL output preserves
query control characters; all published files use LF on every platform.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
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
        lines = path.read_text(encoding="utf-8").split("\n")
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
        if any(char.isspace() for char in qid):
            raise EvaluationError(f"{path}:{line_no}: qid 不能包含空白字符")
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


def _validate_split_policy(seed, dev_fraction, test_fraction):
    if type(seed) is not int:
        raise EvaluationError("seed 必须是整数")
    if not 0 <= dev_fraction < 1 or not 0 <= test_fraction < 1 or dev_fraction + test_fraction >= 1:
        raise EvaluationError("dev/test 比例必须非负且总和小于 1")


def _query_format(queries, requested):
    if requested not in ("auto", "tsv", "jsonl"):
        raise EvaluationError("query_format 必须为 auto、tsv 或 jsonl")
    multiline = any(any(char in query for char in "\t\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")
                    for _, query in queries)
    if requested == "tsv" and multiline:
        raise EvaluationError("查询含制表或换行，必须使用 JSONL 输出")
    return ("jsonl" if multiline else "tsv") if requested == "auto" else requested


def _write_splits(out, queries, qrel_rows, seed, dev_fraction, test_fraction, query_format):
    assignments = {qid: _split_for(qid, seed, dev_fraction, test_fraction) for qid, _ in queries}
    for split in SPLITS:
        with (out / f"queries.{split}.{query_format}").open("w", encoding="utf-8", newline="\n") as handle:
            for qid, query in queries:
                if assignments[qid] == split:
                    if query_format == "jsonl":
                        handle.write(json.dumps({"qid": qid, "query": query}, separators=(",", ":")) + "\n")
                    else:
                        handle.write(f"{qid}\t{query}\n")
        with (out / f"qrels.{split}.tsv").open("w", encoding="utf-8", newline="\n") as handle:
            for qid, docid, relevance in qrel_rows:
                if assignments[qid] == split:
                    handle.write(f"{qid} 0 {docid} {relevance:g}\n")
    return assignments


def _finish_manifest(out, manifest):
    manifest["files"] = {path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
                         for path in sorted(out.iterdir())
                         if path.is_file() and path.name != "dataset_manifest.json"}
    with (out / "dataset_manifest.json").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def _split_sources(queries_path, qrels_path):
    return {key: {"sha256": _sha256(Path(path)), "bytes": Path(path).stat().st_size}
            for key, path in (("queries", queries_path), ("qrels", qrels_path))}


def prepare_query_splits(queries_path: str | Path, qrels_path: str | Path,
                         out_dir: str | Path, *, seed: int = 2026,
                         dev_fraction: float = 0.1, test_fraction: float = 0.8,
                         expected_queries: int | None = None, expected_qrels: int | None = None,
                         query_format: str = "jsonl") -> dict:
    """Split already corpus-validated query/qrels files, without loading a corpus.

    This entry checks query IDs and qrel query references only. It deliberately
    makes no claim about document membership or corpus validation.
    """
    _validate_split_policy(seed, dev_fraction, test_fraction)
    sources = _split_sources(queries_path, qrels_path)
    queries = load_query_records(queries_path)
    qrel_rows = _qrel_rows(Path(qrels_path))
    query_format = _query_format(queries, query_format)
    for label, actual, expected in (("查询", len(queries), expected_queries),
                                    ("qrels", len(qrel_rows), expected_qrels)):
        if expected is not None and actual != expected:
            raise EvaluationError(f"{label}数不符: {actual} != {expected}")
    unknown = {qid for qid, _, _ in qrel_rows} - {qid for qid, _ in queries}
    if unknown:
        raise EvaluationError(f"qrels 含未知 qid: {sorted(unknown)[:3]}")
    out = Path(out_dir)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise EvaluationError(f"输出目录必须不存在或为空: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{out.name}.query-splits-", dir=out.parent) as directory:
        stage = Path(directory)
        assignments = _write_splits(stage, queries, qrel_rows, seed, dev_fraction, test_fraction, query_format)
        manifest = _finish_manifest(stage, {
            "schema_version": 1, "scope": "query-splits-only", "corpus_validation": "not_performed",
            "sources": sources, "seed": seed, "query_format": query_format,
            "split_fractions": {"dev": dev_fraction, "test": test_fraction,
                                "train": 1.0 - dev_fraction - test_fraction},
            "counts": {"queries": len(queries), "qrels": len(qrel_rows)},
            "splits": {split: sum(value == split for value in assignments.values()) for split in SPLITS},
        })
        if _split_sources(queries_path, qrels_path) != sources:
            raise EvaluationError("划分期间输入文件发生变化")
        if out.exists():
            out.rmdir()
        stage.rename(out)
    return manifest


def prepare_dataset(corpus_path: str | Path, queries_path: str | Path,
                    qrels_path: str | Path, out_dir: str | Path, *, seed: int = 2026,
                    dev_fraction: float = 0.1, test_fraction: float = 0.8,
                    expected_docs: int | None = None,
                    expected_queries: int | None = None,
                    expected_qrels: int | None = None, query_format: str = "auto") -> dict:
    """Normalize inputs, write stable split files, and return the manifest."""

    _validate_split_policy(seed, dev_fraction, test_fraction)
    corpus = load_corpus(corpus_path)
    queries = load_query_records(queries_path)
    query_format = _query_format(queries, query_format)
    qrel_rows = _qrel_rows(Path(qrels_path))
    if expected_docs is not None and len(corpus) != expected_docs:
        raise EvaluationError(f"文档数不符: {len(corpus)} != {expected_docs}")
    if expected_queries is not None and len(queries) != expected_queries:
        raise EvaluationError(f"查询数不符: {len(queries)} != {expected_queries}")
    if expected_qrels is not None and len(qrel_rows) != expected_qrels:
        raise EvaluationError(f"qrels 数不符: {len(qrel_rows)} != {expected_qrels}")
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
    with corpus_out.open("w", encoding="utf-8", newline="\n") as handle:
        for record in corpus:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    assignments = _write_splits(out, queries, qrel_rows, seed, dev_fraction, test_fraction, query_format)
    manifest = {
        "schema_version": 1,
        "seed": seed,
        "query_format": query_format,
        "split_fractions": {"dev": dev_fraction, "test": test_fraction,
                             "train": 1.0 - dev_fraction - test_fraction},
        "counts": {"documents": len(corpus), "queries": len(queries),
                    "qrels": len(qrel_rows)},
        "splits": {split: sum(value == split for value in assignments.values())
                   for split in SPLITS},
        "files": {},
    }
    return _finish_manifest(out, manifest)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校验并固定外部检索数据集的输入与划分")
    parser.add_argument("--corpus", type=Path, help="JSONL corpus; normal mode loads it into memory")
    parser.add_argument("--queries-only", action="store_true", help="只划分已完成语料校验的查询/qrels，不读取或复制 corpus")
    parser.add_argument("--query-format", choices=("auto", "tsv", "jsonl"),
                        help="默认普通准备为 auto，仅划分查询为 jsonl；JSONL 保留查询中的制表和换行")
    parser.add_argument("--queries", required=True, type=Path, help="JSONL queries")
    parser.add_argument("--qrels", required=True, type=Path, help="TREC qrels")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--dev-fraction", type=float, default=0.1)
    parser.add_argument("--test-fraction", type=float, default=0.8)
    parser.add_argument("--expected-docs", type=int)
    parser.add_argument("--expected-queries", type=int)
    parser.add_argument("--expected-qrels", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.queries_only and (args.corpus is not None or args.expected_docs is not None):
        parser.error("--queries-only 不接受 --corpus 或 --expected-docs，不执行语料校验")
    if not args.queries_only and args.corpus is None:
        parser.error("普通准备模式必须提供 --corpus，或使用 --queries-only")
    try:
        options = dict(seed=args.seed,
            dev_fraction=args.dev_fraction, test_fraction=args.test_fraction,
            expected_queries=args.expected_queries,
            expected_qrels=args.expected_qrels)
        manifest = (prepare_query_splits(args.queries, args.qrels, args.out,
                                        query_format=args.query_format or "jsonl", **options)
                    if args.queries_only else prepare_dataset(args.corpus, args.queries, args.qrels, args.out,
                                                              expected_docs=args.expected_docs,
                                                              query_format=args.query_format or "auto", **options))
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    except (EvaluationError, OSError) as error:
        print(f"dataset preparation error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
