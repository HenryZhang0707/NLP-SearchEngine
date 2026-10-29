"""Portable regression checks for the optional compact/offline scale path."""

import json
import tempfile
from pathlib import Path

from contracts.conformance import check_reader
from offline.build import build
from offline.compact import CompactIndex, compile_index
from offline.disk_documents import DiskDocuments
from offline.packed_index import PackedIndex
from offline.prepare import prepare


CORPUS = [
    {"doc_id": "d0", "title": "红苹果", "text": "红苹果 手机 进水"},
    {"doc_id": "d1", "title": "蓝苹果", "text": "蓝苹果 手机 充电"},
]


def _write_corpus(path: Path) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in CORPUS),
                    encoding="utf-8")


def test_prepare_compact_and_packed_readers_preserve_contract():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        corpus = root / "corpus.jsonl"
        _write_corpus(corpus)
        prepared = root / "prepared"
        report = prepare(corpus, prepared)
        assert report["accepted"] == len(CORPUS)
        assert json.loads((prepared / "source_manifest.json").read_text(encoding="utf-8"))["accepted"] == 2

        lexical = root / "lexical"
        build(prepared / "corpus.jsonl", lexical, "compact-test")
        compact = root / "compact"
        compile_index(lexical, compact)
        with PackedIndex(compact, cache_bytes=1024) as reader:
            check_reader(reader, CORPUS)
            assert reader.external_doc_id(0) == "d0"
            assert reader.positions_for("手机", 0)
            assert reader.cache_info()["budget_bytes"] == 1024
        with CompactIndex(compact) as reader:
            assert reader.doc(1).doc_id == "d1"
        with DiskDocuments(lexical) as documents:
            assert documents.doc(0).doc_id == "d0"


ALL_TESTS = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
