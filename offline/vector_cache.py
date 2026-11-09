"""Offline, bounded-memory float32 chunks for independently scheduled encoders.

This is a build adapter, not an online/vector-index format. Source validation is
a prerequisite: use CorpusDocuments (verified raw JSONL SHA) or DiskDocuments
(verified lexical inventory) before calling produce_chunks. The caller supplies
the digest of that verified raw document file, never corpus_fingerprint().

Claims are deliberately not stolen, including after a crash. A failed claim and
its partial files are recovery evidence, not a completed chunk. Operators must
resolve such a claim explicitly before retrying that chunk. Completed chunks
are immutable and reusable only after full validation against the real reader.
Tiny .seal hard links are permanent cache metadata: keep them while the cache
exists, since removing an alias changes the published inode's POSIX ctime.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from contracts.retrieval import EmbeddingSpec, RetrievalUnavailable, validate_vector


CACHE_MANIFEST = "vector_cache.json"
CACHE_SCHEMA = 1


def _digest(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _sha_file(handle):
    digest = hashlib.sha256()
    handle.seek(0)
    while block := handle.read(1024 * 1024):
        digest.update(block)
    handle.seek(0)
    return digest.hexdigest()


def _sync(handle):
    handle.flush()
    os.fsync(handle.fileno())


def _directory_sync(path):
    # Windows cannot open a directory this way; file fsync + atomic hard links
    # still enforce publication ordering. POSIX also persists directory entries.
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class CacheConfig:
    raw_source_sha256: str
    n_docs: int
    embedding: EmbeddingSpec
    encoder_contract: str
    source_kind: str = "corpus-jsonl"
    chunk_size: int = 50000  # 3125 * 16; non-final boundaries preserve batch composition
    batch_size: int = 16
    shards: int = 2

    def __post_init__(self):
        if not _digest(self.raw_source_sha256):
            raise ValueError("raw_source_sha256 must be a verified raw-file SHA256")
        for name in ("n_docs", "chunk_size", "batch_size", "shards"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.chunk_size % self.batch_size:
            raise ValueError("chunk_size must be batch aligned")
        if not isinstance(self.embedding, EmbeddingSpec):
            raise ValueError("embedding must be an EmbeddingSpec")
        # Contract identifiers describe encoder code/precision/pooling/truncation;
        # only identifiers, never runtime paths, credentials or shell commands.
        if (not isinstance(self.encoder_contract, str) or not self.encoder_contract
                or len(self.encoder_contract) > 128
                or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                       for c in self.encoder_contract)):
            raise ValueError("encoder_contract must be a logical encoder identifier")
        if self.source_kind not in ("corpus-jsonl", "index-documents-jsonl"):
            raise ValueError("unsupported source_kind")

    def to_dict(self):
        return {"schema_version": CACHE_SCHEMA, "dtype": "float32-le",
                "mapping": "global-docid-external-id-input-utf8-sha256-v1",
                "assignment": "chunk-id-mod-shards", **asdict(self)}

    @property
    def identity(self):
        return hashlib.sha256(_json(self.to_dict())).hexdigest()

    @property
    def n_chunks(self):
        return (self.n_docs + self.chunk_size - 1) // self.chunk_size

    def bounds(self, chunk_id):
        if type(chunk_id) is not int or not 0 <= chunk_id < self.n_chunks:
            raise ValueError("chunk_id outside cache")
        start = chunk_id * self.chunk_size
        return start, min(start + self.chunk_size, self.n_docs)


def _check_reader(reader, config):
    if type(reader.n_docs) is not int or reader.n_docs != config.n_docs:
        raise ValueError("cache/source document count mismatch")


def _document(reader, ordinal):
    doc = reader.doc(ordinal)
    if (type(doc.docid) is not int or doc.docid != ordinal
            or not isinstance(doc.doc_id, str) or not doc.doc_id
            or not isinstance(doc.title, str) or not isinstance(doc.text, str)):
        raise ValueError("invalid source document identity/fields")
    text = doc.title + "\n" + doc.text
    return text, [ordinal, doc.doc_id, hashlib.sha256(text.encode("utf-8")).hexdigest()]


def _prefix(chunk_id):
    return f"chunk-{chunk_id:08d}"


def _publish_config(root, config):
    root.mkdir(parents=True, exist_ok=True)
    target = root / CACHE_MANIFEST
    # The winner keeps this tiny seal link. Removing a staging hard link after
    # target becomes visible would change target's inode ctime on POSIX.
    pending = root / (".cache-config-" + uuid.uuid4().hex + ".seal")
    published = False
    try:
        with pending.open("xb") as output:
            output.write(_json(config.to_dict()))
            _sync(output)
        try:
            os.link(pending, target)
            published = True
            _directory_sync(root)
        except FileExistsError:
            _check_config(root, config)
    finally:
        if not published:
            pending.unlink(missing_ok=True)
    _check_config(root, config)


def _check_config(root, config):
    path = root / CACHE_MANIFEST
    if path.is_symlink() or path.read_bytes() != _json(config.to_dict()):
        raise ValueError("cache configuration/source/spec/assignment mismatch")


def _stat(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns)


class _VerifiedChunk:
    """Validate the entire committed chunk before exposing any of its vectors."""

    def __init__(self, root, config, chunk_id, reader):
        self.handles = []
        self.paths = []
        self.identities = []
        self.path_identities = []
        self.start, self.stop = config.bounds(chunk_id)
        self.position = self.start
        self.spec = config.embedding
        prefix = _prefix(chunk_id)
        try:
            marker = self._open(root / (prefix + ".json"))
            # Markers are small fixed-schema metadata; refuse arbitrary payloads.
            raw = marker.read(65537)
            if len(raw) > 65536:
                raise ValueError("chunk completion marker too large")
            metadata = json.loads(raw)
            expected = {"schema_version": CACHE_SCHEMA,
                        "cache_identity": config.identity, "chunk_id": chunk_id,
                        "start": self.start, "stop": self.stop,
                        "count": self.stop - self.start,
                        "dimension": self.spec.dimension}
            if (not isinstance(metadata, dict)
                    or set(metadata) != set(expected) | {"vectors", "mapping"}
                    or any(type(metadata[k]) is not type(v) or metadata[k] != v
                           for k, v in expected.items())):
                raise ValueError("chunk identity/range/count/dimension mismatch")
            self.vectors = self._open(root / (prefix + ".f32"))
            self.mapping = self._open(root / (prefix + ".ids.jsonl"))
            for key, handle in (("vectors", self.vectors), ("mapping", self.mapping)):
                entry = metadata[key]
                if (not isinstance(entry, dict) or set(entry) != {"bytes", "sha256"}
                        or type(entry["bytes"]) is not int or entry["bytes"] < 0
                        or not _digest(entry["sha256"])
                        or os.fstat(handle.fileno()).st_size != entry["bytes"]
                        or _sha_file(handle) != entry["sha256"]):
                    raise ValueError("chunk file SHA256/size mismatch")
            self.vector_struct = struct.Struct("<" + "f" * self.spec.dimension)
            if os.fstat(self.vectors.fileno()).st_size != (
                    self.stop - self.start) * self.vector_struct.size:
                raise ValueError("chunk vector byte count mismatch")
            for ordinal in range(self.start, self.stop):
                row, vector = self._read()
                if row != _document(reader, ordinal)[1]:
                    raise ValueError("chunk doc_id/input SHA256/order mismatch")
                validate_vector(vector, self.spec)
            if self.mapping.read(1) or self.vectors.read(1):
                raise ValueError("chunk has trailing records")
            self._check_unchanged()
            self.mapping.seek(0)
            self.vectors.seek(0)
        except BaseException:
            self.close()
            raise

    def _open(self, path):
        if path.is_symlink():
            raise ValueError("cache files must not be symlinks")
        handle = path.open("rb")
        self.handles.append(handle)
        self.paths.append(path)
        self.identities.append(_stat(os.fstat(handle.fileno())))
        self.path_identities.append(_stat(path.stat()))
        # fstat/stat ctime may differ on Windows; common fields must agree.
        if self.identities[-1][:4] != self.path_identities[-1][:4]:
            raise ValueError("cache changed while opening")
        return handle

    def _check_unchanged(self):
        for path, handle, identity, path_identity in zip(
                self.paths, self.handles, self.identities, self.path_identities):
            if (_stat(os.fstat(handle.fileno())) != identity
                    or _stat(path.stat()) != path_identity):
                raise ValueError("cache changed while reading")

    def _read(self):
        try:
            row = json.loads(self.mapping.readline())
            vector = list(self.vector_struct.unpack(self.vectors.read(self.vector_struct.size)))
        except (ValueError, struct.error) as error:
            raise ValueError("invalid chunk mapping/vector record") from error
        if (not isinstance(row, list) or len(row) != 3 or type(row[0]) is not int
                or not isinstance(row[1], str) or not row[1] or not _digest(row[2])):
            raise ValueError("invalid chunk mapping record")
        return row, vector

    def next(self, text, reader):
        row, vector = self._read()
        expected_text, expected_row = _document(reader, self.position)
        if text != expected_text or row != expected_row:
            raise ValueError("cached encoder input/doc_id/order mismatch")
        self.position += 1
        return vector

    def close(self):
        for handle in self.handles:
            handle.close()


def produce_chunks(reader, encoder, cache_dir, config: CacheConfig, *, shard=0,
                   verified_source_sha256, evict_written_pages=False):
    """Encode chunks assigned to one shard; return completed/reused chunk IDs.

    ``verified_source_sha256`` is the caller's already-validated raw source
    digest. It must match config; per-document identities are checked on every
    reuse/read. This function neither discovers sources nor downloads models.
    The source and encoder must remain immutable for the entire operation.
    At most one encoder batch plus one vector is retained by the producer.
    """
    _check_reader(reader, config)
    if verified_source_sha256 != config.raw_source_sha256:
        raise ValueError("verified raw source SHA256 mismatch")
    if encoder.spec != config.embedding:
        raise ValueError("encoder spec mismatch")
    if type(shard) is not int or not 0 <= shard < config.shards:
        raise ValueError("shard outside assignment")
    root = Path(cache_dir)
    _publish_config(root, config)
    completed, reused = [], []
    for chunk_id in range(shard, config.n_chunks, config.shards):
        prefix = _prefix(chunk_id)
        marker_path = root / (prefix + ".json")
        if os.path.lexists(marker_path):
            with_chunk = _VerifiedChunk(root, config, chunk_id, reader)
            with_chunk.close()
            reused.append(chunk_id)
            continue
        claim = root / (prefix + ".claim")
        try:
            claim.mkdir()
        except FileExistsError as error:
            # Another writer may have completed between the check and claim.
            if marker_path.exists():
                with_chunk = _VerifiedChunk(root, config, chunk_id, reader)
                with_chunk.close()
                reused.append(chunk_id)
                continue
            raise FileExistsError("chunk already claimed; partial recovery required") from error
        if any(os.path.lexists(root / (prefix + suffix))
               for suffix in (".json", ".f32", ".ids.jsonl", ".seal")):
            # Preserve our empty claim as evidence too; never infer over an
            # orphaned/conflicting publication left by another attempt.
            raise FileExistsError("chunk contains conflicting/orphaned published files")
        # The claim is exclusively ours. Failure intentionally retains it, and
        # publication refuses every existing target, including orphaned data.
        start, stop = config.bounds(chunk_id)
        vector_part, mapping_part = claim / "vectors.part", claim / "mapping.part"
        vector_struct = struct.Struct("<" + "f" * config.embedding.dimension)
        with vector_part.open("xb") as vectors, mapping_part.open("xb") as mapping:
            for batch_start in range(start, stop, config.batch_size):
                batch_stop = min(batch_start + config.batch_size, stop)
                entries = [_document(reader, i) for i in range(batch_start, batch_stop)]
                texts = [entry[0] for entry in entries]
                values = encoder.encode_documents(texts)
                if len(values) != len(entries):
                    raise ValueError("encoder output count mismatch")
                if texts != [entry[0] for entry in entries]:
                    raise ValueError("encoder mutated document inputs")
                if encoder.spec != config.embedding:
                    raise ValueError("encoder spec changed during encoding")
                for (_, row), vector in zip(entries, values):
                    validate_vector(vector, config.embedding)
                    packed = vector_struct.pack(*vector)
                    validate_vector(vector_struct.unpack(packed), config.embedding)
                    vectors.write(packed)
                    mapping.write(_json(row))
                del values, entries, texts
            # Validated disk sources also check immutable-file identity here,
            # including changes that happened during the final model call.
            _document(reader, stop - 1)
            _sync(vectors)
            _sync(mapping)
        metadata = {"schema_version": CACHE_SCHEMA, "cache_identity": config.identity,
                    "chunk_id": chunk_id, "start": start, "stop": stop,
                    "count": stop - start, "dimension": config.embedding.dimension}
        for key, path in (("vectors", vector_part), ("mapping", mapping_part)):
            with path.open("rb") as handle:
                metadata[key] = {"bytes": path.stat().st_size, "sha256": _sha_file(handle)}
        marker_part = claim / "completion.part"
        with marker_part.open("xb") as output:
            output.write(_json(metadata))
            _sync(output)
        # Optional eviction touches only files newly written by this producer.
        # Never drop source pages, shared caches, or change cgroup settings.
        if evict_written_pages and hasattr(os, "posix_fadvise"):
            for path in (vector_part, mapping_part):
                with path.open("rb") as handle:
                    os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        os.link(vector_part, root / (prefix + ".f32"))
        os.link(mapping_part, root / (prefix + ".ids.jsonl"))
        # All data/map staging-link removals must precede completion visibility:
        # unlink changes the shared inode's ctime. Keep one tiny marker seal
        # permanently, so its own ctime also stays stable after publication.
        marker_seal = root / (prefix + ".seal")
        os.link(marker_part, marker_seal)
        for path in (vector_part, mapping_part, marker_part):
            path.unlink()
        _directory_sync(root)
        os.link(marker_seal, marker_path)  # completion is the last publication
        _directory_sync(root)
        claim.rmdir()
        completed.append(chunk_id)
    return {"completed": completed, "reused": reused}


class CachedEncoder:
    """Single-pass build-only TextEncoder consuming global document order.

    Missing chunks wait for at most wait_timeout seconds each, measured with a
    monotonic deadline. A corrupt/stale committed chunk fails immediately.
    No query inference or inference fallback is performed.
    """

    def __init__(self, cache_dir, reader, config: CacheConfig, *, wait_timeout=0.0,
                 poll_interval=0.25):
        for name, value in (("wait_timeout", wait_timeout), ("poll_interval", poll_interval)):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0
                    or (name == "poll_interval" and value == 0)):
                raise ValueError(f"{name} must be finite and {'positive' if name == 'poll_interval' else 'nonnegative'}")
        _check_reader(reader, config)
        self.root, self.reader, self.config = Path(cache_dir), reader, config
        self.spec = config.embedding
        self.wait_timeout, self.poll_interval = wait_timeout, poll_interval
        self.position, self._chunk, self._failed = 0, None, False
        self._wait_for(self.root / CACHE_MANIFEST)
        _check_config(self.root, config)

    def _wait_for(self, path):
        deadline = time.monotonic() + self.wait_timeout
        while not os.path.lexists(path):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("committed vector cache chunk/manifest unavailable")
            time.sleep(min(self.poll_interval, remaining))

    def encode_documents(self, texts):
        if self._failed:
            raise ValueError("cached encoder failed/closed; create a new consumer")
        if (not isinstance(texts, list) or any(not isinstance(text, str) for text in texts)
                or self.position + len(texts) > self.config.n_docs):
            raise ValueError("invalid cached encoder document batch/count")
        result = []
        try:
            if self._chunk is not None:
                self._chunk._check_unchanged()
            for text in texts:
                chunk_id = self.position // self.config.chunk_size
                if self._chunk is None:
                    self._wait_for(self.root / (_prefix(chunk_id) + ".json"))
                    _check_config(self.root, self.config)
                    self._chunk = _VerifiedChunk(self.root, self.config, chunk_id, self.reader)
                result.append(self._chunk.next(text, self.reader))
                self.position += 1
                if self.position == self._chunk.stop:
                    self._chunk._check_unchanged()
                    self._chunk.close()
                    self._chunk = None
            if self._chunk is not None:
                # Return no batch values until all backing descriptors still
                # match. Check per batch/segment, not with O(N) extra stats.
                self._chunk._check_unchanged()
            return result
        except BaseException:
            self.close()
            raise

    def encode_query(self, query):
        raise RetrievalUnavailable("CachedEncoder is build-only; use a real query encoder")

    def close(self):
        if self._chunk is not None:
            self._chunk.close()
            self._chunk = None
        self._failed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
