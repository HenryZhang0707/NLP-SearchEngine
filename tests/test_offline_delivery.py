"""RFC-0004 delivery, provenance and rollback checks."""

import json
import tempfile
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from contracts.index_format import verify_files, load_manifest, sha256_of
from contracts.retrieval import EmbeddingSpec, VECTOR_MANIFEST
from offline.build import build
from offline.build_hybrid import build_hybrid
from offline.compact import compile_index
from offline.disk_index import DiskIndex
from offline.validate import main as validate_main, validate_artifacts, validate_build
from offline.vector_build import build_vectors


CORPUS = [
    {"doc_id": "d0", "title": "red", "text": "red apple"},
    {"doc_id": "d1", "title": "blue", "text": "blue apple"},
]


class Encoder:
    spec = EmbeddingSpec("delivery-test", "v1", dimension=2)

    def encode_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]


class Writer:
    def replace(self, manifest, records):
        self.records = list(records)
        return manifest


class PostPublishFailureWriter(Writer):
    def __init__(self):
        self.rolled_back = False

    def replace(self, manifest, records):
        self.records = list(records)
        return replace(manifest, snapshot_files={"missing.snapshot": "0" * 64})

    def rollback(self, manifest):
        self.rolled_back = True


class FailingEncoder(Encoder):
    def encode_documents(self, texts):
        raise RuntimeError("synthetic encoder failure")


def _corpus(path):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in CORPUS),
                    encoding="utf-8")


def test_hybrid_delivery_writes_provenance_report_and_validates():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        corpus = root / "corpus.jsonl"
        _corpus(corpus)
        build_dir = root / "build"
        with patch.object(DiskIndex, "__init__", side_effect=AssertionError("vector building must not load postings")):
            result = build_hybrid(corpus, build_dir, "delivery", Encoder(), Writer(),
                                  "delivery-v1", backend="memory-exact")
        assert (build_dir / "build_provenance.json").is_file()
        assert (build_dir / "build_report.json").is_file()
        assert result["provenance"]["corpus"]["n_docs"] == 2
        checked = validate_build(build_dir)
        assert checked["n_docs"] == 2
        assert checked["backend"] == "memory-exact"


def test_failed_new_hybrid_build_is_removed():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        corpus = root / "corpus.jsonl"
        _corpus(corpus)
        build_dir = root / "failed"
        try:
            build_hybrid(corpus, build_dir, "delivery", FailingEncoder(), Writer(),
                         "delivery-failed", backend="memory-exact")
        except RuntimeError:
            pass
        else:
            raise AssertionError("编码失败未传播")
        assert not build_dir.exists()


def test_failed_hybrid_build_does_not_leave_partial_files_in_empty_target():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        corpus = root / "corpus.jsonl"
        _corpus(corpus)
        build_dir = root / "precreated"
        build_dir.mkdir()
        build_hybrid_error = None
        try:
            build_hybrid(corpus, build_dir, "delivery", FailingEncoder(), Writer(),
                         "delivery-failed", backend="memory-exact")
        except RuntimeError as error:
            build_hybrid_error = error
        assert build_hybrid_error is not None
        assert build_dir.is_dir()
        assert list(build_dir.iterdir()) == []


def test_strict_delivery_requires_all_lexical_hashes():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        corpus = root / "corpus.jsonl"
        _corpus(corpus)
        index = root / "index"
        build(corpus, index, "delivery")
        manifest_data = json.loads((index / "manifest.json").read_text(encoding="utf-8"))
        del manifest_data["files"]["docs.jsonl"]
        (index / "manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")
        manifest = load_manifest(index)
        try:
            verify_files(index, manifest, require_hashes=True)
        except ValueError:
            return
        raise AssertionError("生产校验接受了缺失文件摘要")


def test_builder_rejects_bad_rows_without_touching_output():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        corpus = root / "bad.jsonl"
        corpus.write_text('{"doc_id":"d0","title":3,"text":"x"}\n', encoding="utf-8")
        out = root / "out"
        try:
            build(corpus, out, "bad")
        except ValueError:
            pass
        else:
            raise AssertionError("非法 title 未被拒绝")
        assert not out.exists()


def test_validator_rejects_report_path_traversal():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        corpus = root / "corpus.jsonl"
        _corpus(corpus)
        build_dir = root / "build"
        build_hybrid(corpus, build_dir, "delivery", Encoder(), Writer(),
                     "delivery-v1", backend="memory-exact")
        report_path = build_dir / "build_report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["artifacts"]["../outside"] = {"sha256": "0" * 64}
        report_path.write_text(json.dumps(report), encoding="utf-8")
        try:
            validate_build(build_dir)
        except ValueError as error:
            assert "路径" in str(error)
            return
        raise AssertionError("validator 接受了越界产物路径")


def test_vector_build_rolls_back_after_writer_returns_before_manifest_publish():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        corpus = root / "corpus.jsonl"
        _corpus(corpus)
        index = root / "index"
        build(corpus, index, "delivery")
        writer = PostPublishFailureWriter()
        try:
            build_vectors(DiskIndex(index), Encoder(), writer, root / "vectors",
                          "delivery-v1", backend="memory-exact")
        except ValueError:
            pass
        else:
            raise AssertionError("损坏的 writer 结果未被拒绝")
        assert writer.rolled_back


def test_validator_rejects_incomplete_report_artifacts():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        corpus = root / "corpus.jsonl"
        _corpus(corpus)
        build_dir = root / "build"
        build_hybrid(corpus, build_dir, "delivery", Encoder(), Writer(),
                     "delivery-v1", backend="memory-exact")
        report_path = build_dir / "build_report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["artifacts"] = {}
        report_path.write_text(json.dumps(report), encoding="utf-8")
        try:
            validate_build(build_dir)
        except ValueError as error:
            assert "必需产物" in str(error)
            return
        raise AssertionError("validator 接受了不完整的产物报告")


def test_validate_separately_published_compact_and_vector_artifacts():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        corpus = root / "corpus.jsonl"
        _corpus(corpus)
        lexical, compact, vectors = (root / name for name in ("lexical", "compact", "vectors"))
        build(corpus, lexical, "delivery")
        compile_index(lexical, compact)

        class SnapshotWriter(Writer):
            def replace(self, manifest, records):
                super().replace(manifest, records)
                snapshot = vectors / "delivery.snapshot"
                snapshot.write_bytes(b"synthetic snapshot for byte verification")
                return replace(manifest, snapshot_files={snapshot.name: sha256_of(snapshot)})

        manifest = build_vectors(DiskIndex(lexical), Encoder(), SnapshotWriter(),
                                 vectors, "delivery-v1", backend="qdrant-hnsw")
        checked = validate_artifacts(lexical, vectors)
        assert checked["n_docs"] == 2
        assert checked["corpus_sha256"] == manifest.corpus_sha256
        assert checked["snapshot_sha256"] == manifest.snapshot_files

        args = ["--lexical-index", str(compact), "--vector-artifacts", str(vectors)]
        with redirect_stdout(StringIO()) as stdout:
            assert validate_main(args) == 0
        assert json.loads(stdout.getvalue()) == checked

        compact_path = compact / "compact.json"
        original_compact = compact_path.read_text(encoding="utf-8")
        damaged_compact = json.loads(original_compact)
        del damaged_compact["source_manifest"]
        compact_path.write_text(json.dumps(damaged_compact), encoding="utf-8")
        with redirect_stdout(StringIO()) as stdout:
            assert validate_main(args) == 2
        assert "源索引清单" in stdout.getvalue()
        compact_path.write_text(original_compact, encoding="utf-8")

        manifest_path = vectors / VECTOR_MANIFEST
        for fields, expected in (
            ({"n_docs": 3}, "n_docs"),
            ({"corpus_sha256": "0" * 64}, "corpus_sha256"),
            ({"snapshot_files": {}}, "快照 SHA256"),
            ({"snapshot_files": {"../delivery.snapshot": "0" * 64}}, "快照只允许"),
            ({"backend": "memory-exact"}, "qdrant-hnsw"),
        ):
            manifest_path.write_text(json.dumps({**manifest.to_dict(), **fields}), encoding="utf-8")
            with redirect_stdout(StringIO()) as stdout:
                assert validate_main(args) == 2
            assert expected in stdout.getvalue()

        manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
        (vectors / "delivery.snapshot").write_bytes(b"damaged snapshot")
        with redirect_stdout(StringIO()) as stdout:
            assert validate_main(args) == 2
        assert "SHA256 不匹配" in stdout.getvalue()


ALL_TESTS = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
