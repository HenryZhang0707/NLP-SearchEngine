"""Durable cleaned news.cn collection. Targets count accepted unique articles.

All --seed and JSONL --seed-file URLs enter at depth zero. Seed-file bytes,
cleaning rules and source files bind resume. No qrels or model scores are read.
Raw cache hashes refer to HTTP response bytes; decoded HTML hashes are separate.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import gzip
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys

from contracts import analyzer
from . import clean_news
from .crawl_resume import DurableCrawl, _fetch_bytes_once, _json
from .crawl_web import CrawlConfig, CrawlError, _PageParser, normalize_url


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if os.name != 'nt':
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class CleanArticleCrawl(DurableCrawl):
    """Injected fetch returns (content_type, charset, original_response_bytes)."""
    def __init__(self, state_dir, seeds, config=None, *, fetch=None, seed_file_sha256=None, **kwargs):
        self.byte_fetch = fetch or _fetch_bytes_once
        super().__init__(state_dir, seeds, config, fetch=self._fetch_cached, **kwargs)
        if self.hosts != {'www.news.cn'}:
            raise CrawlError('clean collection requires www.news.cn seeds')
        self.binding.update(collection='clean-news/v1', rule_version=clean_news.RULE_VERSION,
                            clean_source_sha256=hashlib.sha256(Path(clean_news.__file__).read_bytes()).hexdigest(),
                            collection_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                            analyzer_config_hash=analyzer.config_hash(),
                            analyzer_source_sha256=hashlib.sha256(Path(analyzer.__file__).read_bytes()).hexdigest(),
                            seed_file_sha256=seed_file_sha256, seed_depth=0,
                            dedup_policy='normalized-exact-or-simhash64-hamming3-length0.9-4x16/v1')

    def __enter__(self):
        super().__enter__()
        try:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS signatures(
                    seq INTEGER PRIMARY KEY REFERENCES pages(seq),
                    exact_hash TEXT UNIQUE NOT NULL, simhash TEXT NOT NULL,
                    length INTEGER NOT NULL, b0 INTEGER NOT NULL, b1 INTEGER NOT NULL,
                    b2 INTEGER NOT NULL, b3 INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS sig_b0 ON signatures(b0);
                CREATE INDEX IF NOT EXISTS sig_b1 ON signatures(b1);
                CREATE INDEX IF NOT EXISTS sig_b2 ON signatures(b2);
                CREATE INDEX IF NOT EXISTS sig_b3 ON signatures(b3);
                CREATE TABLE IF NOT EXISTS captures(
                    id INTEGER PRIMARY KEY, attempt INTEGER, source_url TEXT NOT NULL,
                    collected_at REAL NOT NULL, http_bytes_sha256 TEXT NOT NULL,
                    cache_path TEXT NOT NULL, content_type TEXT, charset TEXT);
                CREATE TABLE IF NOT EXISTS evidence(
                    attempt INTEGER PRIMARY KEY, source_url TEXT NOT NULL,
                    outcome TEXT NOT NULL, representative_seq INTEGER,
                    clean_metadata TEXT);
            ''')
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def _fetch_cached(self, url, config):
        kind, charset, raw = self.byte_fetch(url, config)
        if not isinstance(raw, bytes):
            raise CrawlError('byte_fetch_requires_bytes')
        if len(raw) > config.max_bytes:
            raise CrawlError('response_too_large')
        digest = hashlib.sha256(raw).hexdigest()
        relative = Path('raw') / digest[:2] / (digest + '.html.gz')
        path = self.directory / relative
        if not path.exists():
            _atomic(path, gzip.compress(raw, mtime=0))
        else:
            if hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest() != digest:
                raise CrawlError('raw_cache_hash_mismatch')
        attempt = self.db.execute("SELECT id FROM attempts WHERE outcome='INFLIGHT' ORDER BY id DESC LIMIT 1").fetchone()
        with self.db:
            self.db.execute('INSERT INTO captures(attempt,source_url,collected_at,http_bytes_sha256,cache_path,content_type,charset) VALUES (?,?,?,?,?,?,?)',
                            (attempt[0] if attempt else None, normalize_url(url), self.clock(), digest,
                             relative.as_posix(), kind, charset))
        return kind, raw.decode(charset or 'utf-8', errors='replace')

    def _parse_page(self, final_url, body):
        parser = _PageParser()
        parser.feed(body)
        parser.close()
        _, _, links = parser.parse(final_url)
        self.article = clean_news.extract_article(body, final_url)
        return final_url, self.article.title, self.article.text, links

    def _store_page(self, parsed, pages):
        url, title, text, links = parsed
        article = self.article
        representative = None
        if not article.accepted:
            outcome = 'filtered'
        else:
            sig = clean_news.signature(text)
            exact = self.db.execute('SELECT seq FROM signatures WHERE exact_hash=?',
                                    (sig.normalized_text_sha256,)).fetchone()
            bands = [(sig.simhash64 >> (16 * i)) & 0xffff for i in range(4)]
            if exact:
                representative, outcome = exact[0], 'duplicate_exact'
            else:
                outcome = 'success'
                for seq, exact_hash, value, length in self.db.execute(
                        'SELECT seq,exact_hash,simhash,length FROM signatures WHERE b0=? OR b1=? OR b2=? OR b3=? ORDER BY seq', bands):
                    candidate = clean_news.TextSignature(exact_hash, int(value, 16), length)
                    if clean_news.is_near_duplicate(sig, candidate):
                        representative, outcome = seq, 'duplicate_near'
                        break
            if outcome == 'success':
                # A redirected alias can reach an accepted URL with changed content.
                existing = self.db.execute('SELECT seq FROM pages WHERE url=?', (url,)).fetchone()
                if existing:
                    representative, outcome = existing[0], 'duplicate_url'
                else:
                    super()._store_page(parsed, pages)
                    representative = pages + 1
                    self.db.execute('INSERT INTO signatures VALUES (?,?,?,?,?,?,?,?)',
                                    (representative, sig.normalized_text_sha256, f'{sig.simhash64:016x}', sig.length, *bands))
        attempt = self.db.execute("SELECT id FROM attempts WHERE outcome='INFLIGHT' ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.db.execute('INSERT INTO evidence VALUES (?,?,?,?,?)',
                        (attempt, url, outcome, representative, _json(asdict(article.metadata))))
        return outcome

    def _record_failure(self, url, outcome, attempt):
        self.db.execute('INSERT INTO evidence VALUES (?,?,?,?,NULL)', (attempt, url, outcome, None))

    def export(self):
        # Base exports are individually atomic; DB remains authoritative if interrupted
        # between them. Final receipt is published only after the sidecar snapshot.
        receipt = super().export()
        captures = {}
        for row in self.db.execute('SELECT id,attempt,source_url,collected_at,http_bytes_sha256,cache_path,content_type,charset FROM captures ORDER BY id'):
            keys = ('capture_id', 'attempt', 'source_url', 'collected_at', 'http_bytes_sha256', 'cache_path', 'content_type', 'charset')
            captures.setdefault(row[1], []).append(dict(zip(keys, row)))
        data = bytearray()
        for attempt, url, outcome, started, finished in self.db.execute('SELECT id,url,outcome,started,finished FROM attempts ORDER BY id'):
            evidence = self.db.execute('SELECT source_url,outcome,representative_seq,clean_metadata FROM evidence WHERE attempt=?', (attempt,)).fetchone()
            value = dict(attempt=attempt, requested_url=url, outcome=outcome, started=started,
                         finished=finished, captures=captures.get(attempt, []), representative_seq=None, clean_metadata=None)
            if evidence:
                value.update(source_url=evidence[0], outcome=evidence[1], representative_seq=evidence[2],
                             clean_metadata=json.loads(evidence[3]) if evidence[3] else None)
            data.extend((_json(value) + '\n').encode('utf-8'))
        _atomic(self.directory / 'article_sidecar.jsonl', data)
        receipt.update(collection='clean-news/v1', accepted_unique_documents=receipt['documents'],
                       sidecar_sha256=hashlib.sha256(data).hexdigest(),
                       raw_capture_count=self.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0])
        _atomic(self.directory / 'receipt.json', (_json(receipt) + '\n').encode('utf-8'))
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', required=True, type=Path)
    parser.add_argument('--seed', action='append', default=[])
    parser.add_argument('--seed-file', type=Path)
    parser.add_argument('--target-documents', type=int, default=20000)
    parser.add_argument('--max-depth', type=int, default=6)
    parser.add_argument('--max-attempts', type=int, default=100000)
    parser.add_argument('--delay-seconds', type=float, default=.5)
    parser.add_argument('--timeout', type=float, default=8)
    parser.add_argument('--max-bytes', type=int, default=1000000)
    parser.add_argument('--user-agent', default=CrawlConfig().user_agent)
    args = parser.parse_args(argv)
    previous = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, terminate)
    try:
        seeds = args.seed[:]
        seed_hash = None
        if args.seed_file:
            raw = args.seed_file.read_bytes()
            seed_hash = hashlib.sha256(raw).hexdigest()
            for line in raw.decode('utf-8-sig').splitlines():
                if line.strip():
                    seeds.append(json.loads(line)['url'])
        config = CrawlConfig(max_documents=args.target_documents, max_depth=args.max_depth,
                             max_bytes=args.max_bytes, timeout=args.timeout,
                             delay_seconds=args.delay_seconds, user_agent=args.user_agent)
        with CleanArticleCrawl(args.state_dir, seeds, config, max_attempts=args.max_attempts,
                               seed_file_sha256=seed_hash) as crawler:
            print(_json(crawler.run()))
    except KeyboardInterrupt:
        return 130
    except (CrawlError, OSError, sqlite3.Error, ValueError, KeyError) as error:
        # Do not print seed-file values or URL parser exception text.
        print('clean crawl error: ' + type(error).__name__, file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
