"""离线向量数据库构建骨架：批量编码 -> 写入向量库 -> 发布 manifest。

真实模型和 Qdrant 快照上传/发布由适配器实现。本模块不 import online。
"""

import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from copy import copy
from dataclasses import replace
from pathlib import Path

from contracts.index_format import sha256_of
from contracts.reader import DocumentSource
from contracts.retrieval import (
    VECTOR_BUILD_METADATA_KEY, VECTOR_MANIFEST, TextEncoder, VectorManifest,
    VectorRecord, VectorWriter,
    corpus_fingerprint, validate_vector,
)


@contextmanager
def _identity_database(out_dir: Path):
    """Own an isolated SQLite directory, including any SQLite sidecar files."""
    with tempfile.TemporaryDirectory(prefix=".vector-ids-", dir=out_dir) as directory:
        database = sqlite3.connect(Path(directory) / "ids.sqlite")
        try:
            database.execute("CREATE TABLE ids(doc_id TEXT PRIMARY KEY) WITHOUT ROWID")
            yield database
        finally:
            database.close()


def _validate_reader_ids(reader: DocumentSource, out_dir: Path) -> None:
    """Check external-ID uniqueness without retaining an O(N) Python set."""

    with _identity_database(out_dir) as database:
        for docid in range(reader.n_docs):
            doc_id = reader.doc(docid).doc_id
            try:
                database.execute("INSERT INTO ids(doc_id) VALUES (?)", (doc_id,))
            except sqlite3.IntegrityError as error:
                raise ValueError("doc_id 不唯一") from error
        database.commit()


def build_vectors(reader: DocumentSource, encoder: TextEncoder, writer: VectorWriter,
                  out_dir: Path, collection: str, batch_size: int = 32,
                  backend: str = "qdrant-hnsw", index_params: dict | None = None
                  ) -> VectorManifest:
    """仅在 writer 成功消费全部记录后发布元数据；目标目录须为新的构建目录。

    一文档一向量。title + '\\n' + text，前缀/截断由编码器按 spec 执行。
    不允许覆盖已发布的向量 manifest；真实后端也须使用新的 collection。
    """
    out_dir = Path(out_dir)
    target = out_dir / VECTOR_MANIFEST
    if target.exists():
        raise ValueError("向量产物已存在；请使用新的输出目录与 collection")
    if type(batch_size) is not int or batch_size <= 0:
        raise ValueError("batch_size 必须是正整数")
    if index_params is not None and not isinstance(index_params, dict):
        raise ValueError("index_params 必须是 JSON 对象")
    try:
        json.dumps(dict(index_params or {}), sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ValueError("index_params 必须可 JSON 序列化") from error
    out_dir.mkdir(parents=True, exist_ok=True)
    _validate_reader_ids(reader, out_dir)
    manifest = VectorManifest(
        corpus_sha256=corpus_fingerprint(reader), n_docs=reader.n_docs,
        embedding=encoder.spec, collection=collection, backend=backend,
        index_params=dict(index_params or {}),
    )
    consumed = 0

    def records():
        nonlocal consumed
        for start in range(0, reader.n_docs, batch_size):
            docs = [reader.doc(i) for i in range(start, min(start + batch_size, reader.n_docs))]
            vectors = encoder.encode_documents([f"{d.title}\n{d.text}" for d in docs])
            if len(vectors) != len(docs):
                raise ValueError("编码器输出数量与输入文档数不符")
            for doc, vector in zip(docs, vectors):
                validate_vector(vector, encoder.spec)
                consumed += 1
                yield VectorRecord(doc.docid, doc.doc_id, tuple(vector))

    replace_returned = False
    published = None
    try:
        published = writer.replace(manifest, records())
        replace_returned = True
        if consumed != reader.n_docs:
            raise ValueError("writer 未消费全部文档，不能发布 manifest")
        if (not isinstance(published, VectorManifest)
                or replace(published, snapshot_files={}) != manifest):
            raise ValueError("writer 必须返回仅补齐 snapshot_files 的 VectorManifest")
        if backend == "qdrant-hnsw" and not published.snapshot_files:
            raise ValueError("Qdrant 构建必须交付带校验和的快照")
        for name, digest in published.snapshot_files.items():
            snapshot = out_dir / name
            if not snapshot.is_file() or sha256_of(snapshot) != digest:
                raise ValueError("快照不存在或 SHA256 校验失败")
        manifest = published
        # Hard-link publication is atomic and refuses an existing target on
        # Windows/POSIX. Filesystems without hard links fail closed.
        with tempfile.TemporaryDirectory(prefix=".vector-manifest-", dir=out_dir) as directory:
            pending = Path(directory) / target.name
            pending.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
                               encoding="utf-8")
            os.link(pending, target)
        return manifest
    except Exception:
        # A writer may complete a remote build before local manifest
        # publication fails. Adapters that own a remote collection can expose
        # rollback(manifest); legacy writers remain unaffected.
        rollback = getattr(writer, "rollback", None)
        if replace_returned and callable(rollback):
            try:
                rollback(published if isinstance(published, VectorManifest) else manifest)
            except Exception:
                pass
        raise


def _validated_hnsw_params(index_params: dict) -> dict:
    """Validate supported fields against Qdrant 1.16 REST limits, without coercion."""
    if not isinstance(index_params, dict):
        raise ValueError("Qdrant index_params must be a JSON object")
    minimums = {"m": 0, "ef_construct": 4, "full_scan_threshold": 10,
                "max_indexing_threads": 0}
    for name, value in index_params.items():
        if name == "on_disk":
            if type(value) is not bool:
                raise ValueError("HNSW on_disk must be a boolean")
        elif name in minimums:
            if type(value) is not int or value < minimums[name]:
                raise ValueError(f"HNSW {name} must be an integer >= {minimums[name]}")
        else:
            raise ValueError(f"Unsupported HNSW index parameter: {name!r}")
    return dict(index_params)


class QdrantSnapshotWriter:
    """Build a Qdrant collection and publish a verified local snapshot.

    Qdrant and ``qdrant-client`` must support collection metadata (1.16+).
    The SDK is imported lazily so the stdlib lexical workflow keeps working
    without the optional stack. Each build uses a fresh, versioned collection.
    """

    def __init__(self, url: str, snapshot_dir: Path, batch_size: int = 128,
                 timeout: float = 30.0):
        self.url = url
        self.snapshot_dir = Path(snapshot_dir)
        self.batch_size = batch_size
        self.timeout = timeout

    def with_snapshot_dir(self, snapshot_dir: Path) -> "QdrantSnapshotWriter":
        """Scope a writer to a build directory without changing the caller's writer."""
        writer = copy(self)
        writer.snapshot_dir = Path(snapshot_dir)
        return writer

    def replace(self, manifest: VectorManifest, records) -> VectorManifest:
        if self.batch_size <= 0:
            raise ValueError("batch_size 必须为正数")
        hnsw_params = _validated_hnsw_params(manifest.index_params)
        try:
            from qdrant_client import QdrantClient, models
        except ImportError as error:
            raise RuntimeError("QdrantSnapshotWriter 需要安装 qdrant-client") from error
        # An empty mapping deliberately leaves all server defaults untouched.
        hnsw_options = ({"hnsw_config": models.HnswConfigDiff(**hnsw_params)}
                        if hnsw_params else {})
        client = QdrantClient(url=self.url, timeout=self.timeout)
        created = False
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        identity_context = _identity_database(self.snapshot_dir)
        identity_db = identity_context.__enter__()
        count = 0
        batch = []

        def flush() -> None:
            nonlocal batch
            if not batch:
                return
            client.upsert(
                collection_name=manifest.collection,
                points=[models.PointStruct(
                    id=record.docid, vector=list(record.vector),
                    payload={"doc_id": record.doc_id},
                ) for record in batch],
                wait=True,
            )
            batch = []

        try:
            try:
                client.get_collection(manifest.collection)
            except Exception as error:
                if getattr(error, "status_code", None) != 404:
                    raise RuntimeError(f"无法检查 Qdrant collection: {error}") from error
            else:
                raise ValueError(f"Qdrant collection 已存在: {manifest.collection}")
            # A preflight 404 does not reserve the name. Claim ownership only
            # after creation returns successfully; a timeout or conflict cannot
            # establish whose collection now exists, so leave it untouched.
            creation_result = client.create_collection(
                collection_name=manifest.collection,
                vectors_config=models.VectorParams(
                    size=manifest.embedding.dimension, distance=models.Distance.COSINE,
                ),
                metadata={VECTOR_BUILD_METADATA_KEY: manifest.build_metadata()},
                **hnsw_options,
            )
            if creation_result is False:
                raise RuntimeError("Qdrant collection creation was not confirmed")
            created = True
            for record in records:
                validate_vector(record.vector, manifest.embedding)
                if type(record.docid) is not int or record.docid != count:
                    raise ValueError("Qdrant point id 必须按 0..n_docs-1 顺序提供")
                if not isinstance(record.doc_id, str) or not record.doc_id:
                    raise ValueError("外部 doc_id 必须非空且唯一")
                try:
                    identity_db.execute("INSERT INTO ids(doc_id) VALUES (?)", (record.doc_id,))
                except sqlite3.IntegrityError as error:
                    raise ValueError("外部 doc_id 必须非空且唯一") from error
                batch.append(record)
                count += 1
                if count % 10000 == 0:
                    identity_db.commit()
                if len(batch) >= self.batch_size:
                    flush()
            flush()
            identity_db.commit()
            if count != manifest.n_docs:
                raise ValueError("向量记录数与 manifest 不符")
            info = client.get_collection(manifest.collection)
            metadata = getattr(getattr(info, "config", None), "metadata", None)
            if (not isinstance(metadata, dict)
                    or metadata.get(VECTOR_BUILD_METADATA_KEY) != manifest.build_metadata()):
                raise ValueError("Qdrant 未保存构建元数据；需要 Qdrant / qdrant-client 1.16+")
            hnsw_config = getattr(getattr(info, "config", None), "hnsw_config", None)
            for name, expected in hnsw_params.items():
                actual = getattr(hnsw_config, name, None)
                if type(actual) is not type(expected) or actual != expected:
                    raise ValueError(f"Qdrant HNSW parameter was not applied: {name}")
            exact_count = client.count(collection_name=manifest.collection, exact=True).count
            if type(exact_count) is not int or exact_count != manifest.n_docs:
                raise ValueError(f"Qdrant 点数不符: {exact_count} != {manifest.n_docs}")
            published = self._download_snapshot(client, manifest)
            return published
        except Exception:
            delete = getattr(client, "delete_collection", None)
            if created and callable(delete):
                try:
                    delete(collection_name=manifest.collection)
                except Exception:
                    pass
            raise
        finally:
            identity_context.__exit__(None, None, None)

    def rollback(self, manifest: VectorManifest) -> None:
        """Best-effort deletion after local publication fails post-build."""

        try:
            from qdrant_client import QdrantClient
            client = QdrantClient(url=self.url, timeout=self.timeout)
            client.delete_collection(collection_name=manifest.collection)
        except Exception:
            # Preserve the original publication error; the next run uses a
            # fresh collection name.
            pass

    def _download_snapshot(self, client, manifest: VectorManifest) -> VectorManifest:
        """Download through a .part file; only the complete file is published."""
        owned_pending = False
        try:
            snapshot = client.create_snapshot(collection_name=manifest.collection)
            snapshot_name = getattr(snapshot, "name", None) or getattr(snapshot, "result", None)
            # Reject path syntax for both Windows and POSIX, even when the
            # current host would interpret the other platform's syntax literally.
            reserved = {"CON", "PRN", "AUX", "NUL"} | {
                f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³"}
            if (not isinstance(snapshot_name, str) or not snapshot_name
                    or snapshot_name in (".", "..")
                    or any(ord(c) < 32 or c in '/\\:<>"|?*' for c in snapshot_name)
                    or snapshot_name.rstrip(" .") != snapshot_name
                    or snapshot_name.partition(".")[0].rstrip(" ").upper() in reserved):
                raise ValueError("Qdrant 未返回有效快照文件名")
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            target = self.snapshot_dir / snapshot_name
            pending = target.with_name(target.name + ".part")
            if os.path.lexists(target):
                raise FileExistsError(f"快照产物已存在: {target.name}")
            from urllib.request import urlopen
            import urllib.parse
            endpoint = (self.url.rstrip("/") + "/collections/" +
                        urllib.parse.quote(manifest.collection, safe="") + "/snapshots/" +
                        urllib.parse.quote(target.name, safe=""))
            with pending.open("xb") as output:
                owned_pending = True
                with urlopen(endpoint, timeout=self.timeout) as response:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
            expected_size = getattr(snapshot, "size", None)
            if type(expected_size) is int and pending.stat().st_size != expected_size:
                raise ValueError("快照下载大小与 Qdrant 声明不符")
            published = replace(manifest, snapshot_files={target.name: sha256_of(pending)})
            os.link(pending, target)
        except Exception as error:
            raise RuntimeError(f"Qdrant 快照发布失败: {error}") from error
        finally:
            if owned_pending:
                pending.unlink(missing_ok=True)
        return published
