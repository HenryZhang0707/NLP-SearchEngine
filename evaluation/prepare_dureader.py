"""Convert the public DuReader-Retrieval baseline release to project inputs.

The RocketQA baseline archive stores the passage collection as four tab-separated
files and uses ``passage2id.map.json`` to map the global line number to the
stable passage ID.  The official development file is JSONL and contains the
positive answer paragraphs for each query.  This adapter keeps the conversion
streaming with respect to the multi-gigabyte passage collection and never
places the external data in the repository.

Example::

    python -m evaluation.prepare_dureader \
      --root /data/dureader-retrieval-baseline-dataset \
      --out /data/prepared/duretrieval-official-dev \
      --archive /data/dureader-retrieval-baseline-dataset.tar.gz \
      --archive-sha256 <sha256>

The output is an official ``dev`` evaluation split.  It is deliberately not
renamed to ``test``: the public baseline archive does not contain the official
test queries or labels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Iterable, Sequence

from .evaluate import EvaluationError


DEFAULT_ARCHIVE_URL = (
    "https://dataset-bj.cdn.bcebos.com/qianyan/"
    "dureader-retrieval-baseline-dataset.tar.gz"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_file(path: Path) -> Path:
    if not path.is_file():
        raise EvaluationError(f"官方 DuReader 文件不存在: {path}")
    return path


def _load_index_map(path: Path) -> tuple[dict[int, str], set[str]]:
    """Load the official line-index to passage-id map with strict validation."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvaluationError(f"无法读取 passage2id.map.json: {path}") from error
    if not isinstance(value, dict) or not value:
        raise EvaluationError("passage2id.map.json 必须是非空对象")
    result: dict[int, str] = {}
    seen_docids: set[str] = set()
    for raw_index, raw_docid in value.items():
        if not isinstance(raw_index, str) or not raw_index.isdigit():
            raise EvaluationError("passage2id.map.json 含非法行号")
        if not isinstance(raw_docid, str) or not raw_docid.strip():
            raise EvaluationError("passage2id.map.json 含空 passage ID")
        index = int(raw_index)
        if index in result or raw_docid in seen_docids:
            raise EvaluationError("passage2id.map.json 含重复行号或 passage ID")
        result[index] = raw_docid
        seen_docids.add(raw_docid)
    if set(result) != set(range(len(result))):
        raise EvaluationError("passage2id.map.json 行号不是从 0 连续编号")
    return result, seen_docids


def _passage_parts(root: Path) -> list[Path]:
    directory = root / "passage-collection"
    parts = sorted(directory.glob("part-*"))
    if not parts:
        raise EvaluationError(f"未找到官方 passage 分片: {directory}")
    expected = [directory / f"part-{index:02d}" for index in range(4)]
    if parts != expected:
        raise EvaluationError("官方 passage 分片必须恰好为 part-00 到 part-03")
    return [_require_file(path) for path in parts]


def _read_dev(root: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str, float]], dict]:
    path = _require_file(root / "dev" / "dev.json")
    qmap_path = _require_file(root / "dev" / "q2qid.dev.json")
    try:
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        qmap = json.loads(qmap_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvaluationError("官方 dev 文件不是有效 UTF-8 JSON/JSONL") from error
    if not isinstance(qmap, dict):
        raise EvaluationError("q2qid.dev.json 必须是对象")
    queries: list[tuple[str, str]] = []
    qrels: list[tuple[str, str, float]] = []
    seen_qids: set[str] = set()
    seen_qrels: set[tuple[str, str]] = set()
    for line_no, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise EvaluationError(f"dev/dev.json:{line_no}: 记录必须是对象")
        qid = record.get("question_id")
        query = record.get("question")
        answers = record.get("answer_paragraphs")
        if not isinstance(qid, str) or not qid.strip() or not isinstance(query, str) or not query.strip():
            raise EvaluationError(f"dev/dev.json:{line_no}: 缺少 question_id/question")
        if not isinstance(answers, list) or not answers:
            raise EvaluationError(f"dev/dev.json:{line_no}: answer_paragraphs 为空")
        if qid in seen_qids:
            raise EvaluationError(f"dev/dev.json:{line_no}: question_id 重复")
        if qmap.get(query) != qid:
            raise EvaluationError(f"dev/dev.json:{line_no}: q2qid.dev.json 映射不一致")
        seen_qids.add(qid)
        queries.append((qid, query))
        for answer in answers:
            if not isinstance(answer, dict) or not isinstance(answer.get("paragraph_id"), str):
                raise EvaluationError(f"dev/dev.json:{line_no}: 非法 paragraph_id")
            docid = answer["paragraph_id"].strip()
            if not docid:
                raise EvaluationError(f"dev/dev.json:{line_no}: 空 paragraph_id")
            key = (qid, docid)
            if key not in seen_qrels:
                seen_qrels.add(key)
                qrels.append((qid, docid, 1.0))
    if len(queries) != len(qmap):
        raise EvaluationError("q2qid.dev.json 与 dev.json 的查询数不一致")
    if not queries or not qrels:
        raise EvaluationError("官方 dev 数据为空")
    return queries, qrels, {"queries": len(queries), "qrels": len(qrels)}


def _write_queries(path: Path, queries: Sequence[tuple[str, str]]) -> dict:
    digest = hashlib.sha256()
    with path.open("wb") as handle:
        for qid, query in queries:
            # Escape embedded newlines and Unicode line separators so even
            # splitlines()-based clients see exactly one physical row per query.
            line = (json.dumps({"qid": qid, "query": query}, ensure_ascii=True,
                               separators=(",", ":")) + "\n").encode("utf-8")
            handle.write(line)
            digest.update(line)
    return {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def _write_qrels(path: Path, qrels: Sequence[tuple[str, str, float]]) -> dict:
    digest = hashlib.sha256()
    with path.open("wb") as handle:
        for qid, docid, relevance in qrels:
            line = f"{qid} 0 {docid} {relevance:g}\n".encode("utf-8")
            handle.write(line)
            digest.update(line)
    return {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def _write_corpus(path: Path, parts: Sequence[Path], index_map: dict[int, str]) -> dict:
    digest = hashlib.sha256()
    line_index = 0
    seen_docids: set[str] = set()
    with path.open("wb") as output:
        for part in parts:
            with part.open("r", encoding="utf-8", newline="") as source:
                for source_line, raw in enumerate(source, 1):
                    fields = raw.rstrip("\r\n").split("\t")
                    if len(fields) < 3:
                        raise EvaluationError(f"{part}:{source_line}: passage 至少需要 3 列")
                    docid = index_map.get(line_index)
                    if docid is None:
                        raise EvaluationError(f"passage 行号超出 map: {line_index}")
                    text = fields[2]
                    if not text.strip():
                        raise EvaluationError(f"{part}:{source_line}: passage 正文为空")
                    if docid in seen_docids:
                        raise EvaluationError(f"重复 passage ID: {docid}")
                    seen_docids.add(docid)
                    record = {"doc_id": docid, "title": "", "text": text}
                    encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                    output.write(encoded)
                    digest.update(encoded)
                    line_index += 1
    if line_index != len(index_map):
        raise EvaluationError(f"passage 行数与 map 不一致: {line_index} != {len(index_map)}")
    return {"sha256": digest.hexdigest(), "bytes": path.stat().st_size, "documents": line_index}


def prepare_dureader(root: str | Path, out: str | Path, *, archive: str | Path | None = None,
                     archive_sha256: str | None = None, expected_queries: int | None = None,
                     expected_qrels: int | None = None) -> dict:
    root = Path(root)
    out = Path(out)
    if not root.is_dir():
        raise EvaluationError(f"官方 DuReader 根目录不存在: {root}")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise EvaluationError(f"输出目录必须不存在或为空: {out}")
    if archive_sha256 is not None:
        if archive is None:
            raise EvaluationError("--archive-sha256 必须与 --archive 一起提供")
        expected_hash = archive_sha256.lower()
        if len(expected_hash) != 64 or any(char not in "0123456789abcdef" for char in expected_hash):
            raise EvaluationError("archive_sha256 必须是 64 位十六进制")
    archive_info = None
    if archive is not None:
        archive_path = _require_file(Path(archive))
        archive_info = {"path": str(archive_path), "sha256": _sha256(archive_path), "bytes": archive_path.stat().st_size}
        if archive_sha256 is not None and archive_info["sha256"] != archive_sha256.lower():
            raise EvaluationError("下载包 SHA256 与期望值不一致")
    index_path = _require_file(root / "passage-collection" / "passage2id.map.json")
    index_map, passage_ids = _load_index_map(index_path)
    parts = _passage_parts(root)
    queries, qrels, dev_counts = _read_dev(root)
    missing_qrel_ids = sorted({docid for _, docid, _ in qrels if docid not in passage_ids})
    if missing_qrel_ids:
        sample = ", ".join(missing_qrel_ids[:3])
        raise EvaluationError(f"qrels 引用了不存在的 passage ID ({len(missing_qrel_ids)}): {sample}")
    if expected_queries is not None and len(queries) != expected_queries:
        raise EvaluationError(f"官方 dev 查询数不符: {len(queries)} != {expected_queries}")
    if expected_qrels is not None and len(qrels) != expected_qrels:
        raise EvaluationError(f"官方 dev qrels 数不符: {len(qrels)} != {expected_qrels}")
    out.mkdir(parents=True, exist_ok=True)
    corpus_info = _write_corpus(out / "corpus.jsonl", parts, index_map)
    query_info = _write_queries(out / "queries.dev.jsonl", queries)
    qrel_info = _write_qrels(out / "qrels.dev.tsv", qrels)
    manifest = {
        "schema_version": 1,
        "dataset": "DuReader-Retrieval official baseline archive",
        "source_url": DEFAULT_ARCHIVE_URL,
        "source_archive": archive_info,
        "source_root": str(root),
        "source_files": {
            "passage_map": {"path": str(index_path), "sha256": _sha256(index_path), "bytes": index_path.stat().st_size},
            "passage_parts": [str(path) for path in parts],
            "dev_json": str(root / "dev" / "dev.json"),
            "q2qid": str(root / "dev" / "q2qid.dev.json"),
        },
        "split": "dev",
        "split_policy": "official dev only; public baseline archive has no official test labels",
        "official_metrics": ["MRR@10", "Recall@1", "Recall@50"],
        "counts": {"documents": corpus_info["documents"], **dev_counts},
        "files": {
            "corpus.jsonl": corpus_info,
            "queries.dev.jsonl": query_info,
            "qrels.dev.tsv": qrel_info,
        },
    }
    (out / "dataset_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="转换官方 DuReader-Retrieval baseline dev 数据")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--archive-sha256")
    parser.add_argument("--expected-queries", type=int)
    parser.add_argument("--expected-qrels", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = prepare_dureader(
            args.root, args.out, archive=args.archive, archive_sha256=args.archive_sha256,
            expected_queries=args.expected_queries, expected_qrels=args.expected_qrels)
    except (EvaluationError, OSError) as error:
        print(f"dureader preparation error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
