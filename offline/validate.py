"""Validate published offline index artifacts without online imports."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Sequence

from contracts.index_format import MANIFEST, REQUIRED_FILES, load_manifest, sha256_of, verify_files
from contracts.reader import DocumentSource
from contracts.retrieval import VECTOR_MANIFEST, VectorManifest, corpus_fingerprint
from .disk_index import DiskIndex
from .provenance import PROVENANCE_FILE, PROVENANCE_SCHEMA_VERSION, REPORT_FILE


def _safe_artifact_path(root: Path, relative: str) -> Path:
    """Resolve a report/manifest path while rejecting traversal and absolutes."""

    if not isinstance(relative, str) or not relative:
        raise ValueError("交付产物路径必须是非空字符串")
    rel = Path(relative)
    if rel.anchor or ".." in rel.parts:
        raise ValueError(f"交付产物路径非法: {relative}")
    resolved_root = root.resolve()
    resolved = (root / rel).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError(f"交付产物路径越界: {relative}") from error
    return resolved


def _is_sha256(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


def _load_vector_manifest(vector_path: Path) -> VectorManifest:
    if not vector_path.is_file():
        raise ValueError("双索引缺少 vector_manifest.json")
    try:
        return VectorManifest.from_dict(json.loads(vector_path.read_text(encoding="utf-8")))
    except (AttributeError, KeyError, TypeError) as error:
        raise ValueError("vector_manifest.json 格式错误") from error


def _verify_vector_artifacts(root: Path, vector_manifest: VectorManifest,
                             reader: DocumentSource) -> dict[str, str]:
    if vector_manifest.backend == "qdrant-hnsw" and not vector_manifest.snapshot_files:
        raise ValueError("生产 Qdrant 产物必须声明快照 SHA256")
    if vector_manifest.n_docs != reader.n_docs:
        raise ValueError("向量 n_docs 与 BM25 不一致")
    if vector_manifest.corpus_sha256 != corpus_fingerprint(reader):
        raise ValueError("向量 corpus_sha256 与 BM25 不一致")
    snapshot_hashes = {}
    for name, digest in vector_manifest.snapshot_files.items():
        if Path(name).name != name:
            raise ValueError(f"快照路径非法: {name}")
        path = _safe_artifact_path(root, name)
        if not path.is_file():
            raise ValueError(f"快照路径非法或不存在: {name}")
        actual = sha256_of(path)
        if actual != digest:
            raise ValueError(f"快照 SHA256 不匹配: {name}")
        snapshot_hashes[name] = actual
    return snapshot_hashes


def validate_artifacts(lexical_index: str | Path, vector_artifacts: str | Path) -> dict:
    """Check separate A1/compact and Qdrant artifact directories in place.

    Checks offline file hashes and ordered document identity. Restore and
    collection contents are verified separately against a live Qdrant service.
    """
    lexical_root, vector_root = Path(lexical_index), Path(vector_artifacts)
    vector_manifest = _load_vector_manifest(_safe_artifact_path(vector_root, VECTOR_MANIFEST))
    if vector_manifest.backend != "qdrant-hnsw":
        raise ValueError("独立向量交付必须使用 qdrant-hnsw")

    compact_manifest = _safe_artifact_path(lexical_root, "compact.json")
    is_compact = compact_manifest.is_file()
    if is_compact:
        metadata = json.loads(compact_manifest.read_text(encoding="utf-8"))
        files = metadata.get("files") if isinstance(metadata, dict) else None
        if not isinstance(files, dict):
            raise ValueError("compact.json 缺少文件摘要")
        if not isinstance(metadata.get("source_manifest"), dict):
            raise ValueError("compact.json 缺少源索引清单")
        for name in files:
            _safe_artifact_path(lexical_root, name)
    else:
        for name in (MANIFEST, *REQUIRED_FILES):
            _safe_artifact_path(lexical_root, name)
        verify_files(lexical_root, load_manifest(lexical_root), require_hashes=True)

    # PackedIndex always checks its hashes. The strict A1 check above already did so.
    reader = DiskIndex(lexical_root, verify_checksums=False)
    try:
        snapshot_hashes = _verify_vector_artifacts(vector_root, vector_manifest, reader)
        return {
            "dataset": reader.manifest()["dataset"],
            "n_docs": reader.n_docs,
            "corpus_sha256": vector_manifest.corpus_sha256,
            "backend": vector_manifest.backend,
            "snapshot_sha256": snapshot_hashes,
        }
    finally:
        if is_compact:
            reader.close()


def validate_build(build_dir: str | Path) -> dict:
    """Fail closed unless A1, vector identity, snapshot hashes and reports align."""

    root = Path(build_dir)
    lexical_dir = root / "bm25"
    lexical_manifest = load_manifest(lexical_dir)
    verify_files(lexical_dir, lexical_manifest, require_hashes=True)
    reader = DiskIndex(lexical_dir)
    vector_manifest = _load_vector_manifest(root / VECTOR_MANIFEST)
    snapshot_hashes = _verify_vector_artifacts(root, vector_manifest, reader)
    for required in (PROVENANCE_FILE, REPORT_FILE):
        if not (root / required).is_file():
            raise ValueError(f"交付缺少 {required}")
    provenance = json.loads((root / PROVENANCE_FILE).read_text(encoding="utf-8"))
    corpus = provenance.get("corpus") if isinstance(provenance, dict) else None
    source = provenance.get("source") if isinstance(provenance, dict) else None
    embedding = provenance.get("embedding") if isinstance(provenance, dict) else None
    vector = provenance.get("vector") if isinstance(provenance, dict) else None
    expected_embedding = dict(vector_manifest.embedding.__dict__)
    expected_vector = {
        "collection": vector_manifest.collection,
        "backend": vector_manifest.backend,
        "index_params": dict(vector_manifest.index_params),
        "snapshot_files": dict(vector_manifest.snapshot_files),
    }
    source_filename = source.get("filename") if isinstance(source, dict) else None
    if (not isinstance(corpus, dict)
            or provenance.get("schema_version") != PROVENANCE_SCHEMA_VERSION
            or provenance.get("dataset") != lexical_manifest.dataset
            or corpus.get("ordered_fingerprint") != vector_manifest.corpus_sha256
            or corpus.get("n_docs") != reader.n_docs
            or not isinstance(source, dict)
            or not isinstance(source_filename, str)
            or Path(source_filename).name != source_filename
            or not isinstance(source.get("bytes"), int) or source.get("bytes") < 0
            or not _is_sha256(source.get("sha256"))
            or embedding != expected_embedding
            or vector != expected_vector
            or provenance.get("bm25") != lexical_manifest.bm25.to_dict()):
        raise ValueError("provenance 与向量 corpus 指纹不一致")
    report = json.loads((root / REPORT_FILE).read_text(encoding="utf-8"))
    artifacts = report.get("artifacts") if isinstance(report, dict) else None
    if (not isinstance(report, dict)
            or report.get("schema_version") != PROVENANCE_SCHEMA_VERSION
            or report.get("dataset") != lexical_manifest.dataset
            or report.get("n_docs") != reader.n_docs
            or not isinstance(artifacts, dict)):
        raise ValueError("成本报告缺少 artifacts 对象")
    required_artifacts = {
        *(f"bm25/{name}" for name in [MANIFEST, *REQUIRED_FILES]),
        VECTOR_MANIFEST,
        PROVENANCE_FILE,
        *vector_manifest.snapshot_files,
    }
    missing = sorted(required_artifacts - set(artifacts))
    if missing:
        raise ValueError(f"成本报告缺少必需产物: {', '.join(missing)}")
    for relative, entry in artifacts.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
            raise ValueError(f"成本报告缺少产物摘要: {relative}")
        path = _safe_artifact_path(root, relative)
        if not path.is_file():
            raise ValueError(f"成本报告引用的产物不存在: {relative}")
        if entry.get("sha256") != sha256_of(path):
            raise ValueError(f"成本报告哈希不匹配: {relative}")
    return {
        "dataset": lexical_manifest.dataset,
        "n_docs": reader.n_docs,
        "corpus_sha256": vector_manifest.corpus_sha256,
        "backend": vector_manifest.backend,
        "snapshot_sha256": snapshot_hashes,
        "provenance": str(root / PROVENANCE_FILE),
        "report": str(root / REPORT_FILE),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验已发布的离线双索引产物")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--build-dir", type=Path, help="含 provenance 和成本报告的双索引发布目录")
    source.add_argument("--lexical-index", type=Path, help="独立发布的 A1 或 compact 索引目录")
    parser.add_argument("--vector-artifacts", type=Path, help="含 Qdrant manifest 和快照的目录")
    args = parser.parse_args(argv)
    if (args.lexical_index is None) != (args.vector_artifacts is None):
        parser.error("--lexical-index 和 --vector-artifacts 必须同时提供")
    try:
        result = (validate_build(args.build_dir) if args.build_dir is not None
                  else validate_artifacts(args.lexical_index, args.vector_artifacts))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, TypeError, ValueError, sqlite3.Error) as error:
        print(f"validation error: {error}")
        return 2
    except (AttributeError, KeyError):
        print("validation error: 产物清单格式错误")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
