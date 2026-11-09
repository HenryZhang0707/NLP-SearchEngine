"""Read frozen standard corpus JSONL without waiting for a lexical index."""

import hashlib
import json
import os
import threading
from array import array
from pathlib import Path

from contracts.reader import Document


class CorpusDocuments:
    """Validate raw SHA256, row count and fields in one streaming scan.

    Internal IDs are physical JSONL row numbers starting at zero. Blank rows
    are rejected so lexical and vector builds cannot silently disagree on IDs.
    Only two uint64 arrays remain in memory (16 bytes per document, plus array
    allocation overhead); scan/read working memory depends on the largest row.
    No lexical files are required or generated. External-ID uniqueness is NOT
    promised here: build_vectors checks it with its existing temporary SQLite
    table before encoding or publication.

    The input must stay immutable while open. Descriptor and path identity,
    size, mtime and ctime are checked before/after the scan and every read.
    These checks detect observable changes, not adversarial changes that retain
    filesystem metadata. This is not an immutable filesystem snapshot.
    """

    def __init__(self, corpus_path, *, expected_sha256, expected_n_docs):
        if (not isinstance(expected_sha256, str) or len(expected_sha256) != 64
                or any(c not in "0123456789abcdef" for c in expected_sha256)):
            raise ValueError("expected_sha256 must be a lowercase SHA256 digest")
        if type(expected_n_docs) is not int or expected_n_docs <= 0:
            raise ValueError("expected_n_docs must be a positive integer")
        self._path = Path(corpus_path).absolute()
        self._lock = threading.Lock()
        self._starts, self._lengths = array('Q'), array('Q')
        self._file = self._path.open('rb')
        self._invalid = False
        try:
            self._identity = self._stat_identity(os.fstat(self._file.fileno()))
            self._path_identity = self._stat_identity(self._path.stat())
            # Windows fstat and stat may expose different ctime semantics.
            # Compare common identity fields across APIs, but monitor each
            # API's full metadata against its own frozen baseline.
            if self._identity[:4] != self._path_identity[:4]:
                raise ValueError("corpus source changed while opening")
            self._assert_unchanged()
            digest = hashlib.sha256()
            while True:
                start = self._file.tell()
                line = self._file.readline()
                if not line:
                    break
                digest.update(line)
                self._parse_row(line, len(self._starts))
                self._starts.append(start)
                self._lengths.append(len(line))
            self._assert_unchanged()
            if len(self._starts) != expected_n_docs:
                raise ValueError("corpus document count mismatch")
            if digest.hexdigest() != expected_sha256:
                raise ValueError("corpus SHA256 mismatch")
            self.n_docs = expected_n_docs
        except BaseException:
            self._file.close()
            raise

    @staticmethod
    def _stat_identity(value):
        return (value.st_dev, value.st_ino, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns)

    def _assert_unchanged(self):
        if self._invalid:
            raise ValueError("corpus source changed while open")
        if self._file.closed:
            raise ValueError("corpus document source is closed")
        try:
            unchanged = (self._stat_identity(os.fstat(self._file.fileno()))
                         == self._identity
                         and self._stat_identity(self._path.stat())
                         == self._path_identity)
        except OSError as error:
            self._invalid = True
            self._file.close()
            raise ValueError("corpus source changed while open") from error
        if not unchanged:
            self._invalid = True
            self._file.close()
            raise ValueError("corpus source changed while open")

    @staticmethod
    def _parse_row(line, docid):
        try:
            row = json.loads(line.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid corpus JSON at row {docid + 1}") from error
        if (not isinstance(row, dict) or not isinstance(row.get('doc_id'), str)
                or not row['doc_id'] or not isinstance(row.get('title'), str)
                or not isinstance(row.get('text'), str)):
            raise ValueError(f"invalid corpus fields at row {docid + 1}")
        return Document(docid, row['doc_id'], row['title'], row['text'])

    def doc(self, docid: int) -> Document:
        if type(docid) is not int or not 0 <= docid < self.n_docs:
            raise IndexError("document ID outside source")
        with self._lock:
            self._assert_unchanged()
            self._file.seek(self._starts[docid])
            line = self._file.read(self._lengths[docid])
            document = self._parse_row(line, docid)
            self._assert_unchanged()
            return document

    def close(self):
        with self._lock:
            self._file.close()

    def __enter__(self):
        with self._lock:
            self._assert_unchanged()
        return self

    def __exit__(self, *_):
        self.close()
