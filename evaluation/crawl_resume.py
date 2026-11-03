"""Durable FIFO crawler. SQLite is authoritative; exports are replaceable snapshots.

Run with ``python -m evaluation.crawl_resume --state-dir ... --seed ...``.
No annotations or evaluation labels are read or produced.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from . import crawl_web
from .crawl_web import CrawlConfig, CrawlError, _PageParser, normalize_url


CLEANING_VERSION = "crawl_web._PageParser/v1"


class StateLocked(CrawlError):
    pass


class RobotsUnavailable(CrawlError):
    """No usable robots policy: stop safely, retaining the page for resume."""


@contextmanager
def exclusive_lock(directory):
    """OS lock survives stale lock files and is released by process death."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "crawl.lock").open("a+b")
    locked = False
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b" ")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise StateLocked("state directory is already locked") from error
        locked = True
        handle.seek(1)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class _Redirect(Exception):
    def __init__(self, target):
        self.target = target


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_bytes_once(url, config):
    """One HTTP request; caller checks redirects *before* sending the next one."""
    request = Request(url, headers={"User-Agent": config.user_agent,
                                   "Accept": "text/html,text/plain"})
    try:
        response = build_opener(_NoRedirect()).open(request, timeout=config.timeout)
    except HTTPError as error:
        if error.code in (301, 302, 303, 307, 308) and error.headers.get("Location"):
            target = error.headers["Location"]
            error.close()
            raise _Redirect(target)
        error.close()
        raise
    with response:
        body = response.read(config.max_bytes + 1)
        if len(body) > config.max_bytes:
            raise CrawlError("response_too_large")
        return (response.headers.get_content_type(),
                response.headers.get_content_charset() or "utf-8", body)


def _fetch_once(url, config):
    kind, charset, body = _fetch_bytes_once(url, config)
    return kind, body.decode(charset, errors="replace")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DurableCrawl:
    """Use under ``with``; injected callbacks never require real network or sleep.

    fetch is a *single-request* callback (url, config) -> (content_type, body).
    robots is optionally (url) -> bool or (allowed, required_interval_seconds).
    Production robots are fetched through the same persistent rate limiter.
    """
    def __init__(self, state_dir, seeds, config=None, *, max_attempts=100000,
                 fetch=None, robots=None, clock=time.time, sleep=time.sleep,
                 export_every=100):
        self.directory = Path(state_dir)
        self.config = config or CrawlConfig(max_documents=20000, max_depth=6, timeout=8)
        if not self.config.same_domain or self.config.allow_robots_failure:
            raise CrawlError("durable crawl requires same-domain and fail-closed robots")
        if (not math.isfinite(self.config.timeout)
                or not math.isfinite(self.config.delay_seconds)):
            raise CrawlError("timing values must be finite")
        if type(max_attempts) is not int or max_attempts <= 0:
            raise CrawlError("max_attempts must be a positive integer")
        if type(export_every) is not int or export_every <= 0:
            raise CrawlError("export_every must be a positive integer")
        self.seeds = list(dict.fromkeys(normalize_url(url) for url in seeds))
        if not self.seeds:
            raise CrawlError("at least one seed is required")
        self.hosts = {urlsplit(url).netloc for url in self.seeds}
        self.max_attempts, self.export_every = max_attempts, export_every
        self.fetch = fetch or _fetch_once
        self.robots_callback = robots
        self.clock, self.sleep = clock, sleep
        self.robots_cache = {}
        self.binding = dict(seeds=self.seeds, config=asdict(self.config),
                            max_attempts=max_attempts, cleaning_version=CLEANING_VERSION,
                            cleaning_source_sha256=hashlib.sha256(
                                Path(crawl_web.__file__).read_bytes()).hexdigest(),
                            crawler_source_sha256=hashlib.sha256(
                                Path(__file__).read_bytes()).hexdigest(),
                            robots_policy="fail-closed/all-requests-paced/v1")

    def __enter__(self):
        self.lock = exclusive_lock(self.directory)
        self.lock.__enter__()
        try:
            self.db = sqlite3.connect(self.directory / "state.sqlite3")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS frontier(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE NOT NULL,
                    depth INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING');
                CREATE TABLE IF NOT EXISTS attempts(
                    id INTEGER PRIMARY KEY, url TEXT NOT NULL, started REAL NOT NULL,
                    outcome TEXT NOT NULL, finished REAL);
                CREATE TABLE IF NOT EXISTS pages(
                    seq INTEGER PRIMARY KEY, doc_id TEXT UNIQUE NOT NULL,
                    url TEXT UNIQUE NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL,
                    links TEXT NOT NULL);
            """)
            old = self.db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
            if old and old[0] != _json(self.binding):
                raise CrawlError("resume configuration mismatch")
            with self.db:
                if not old:
                    self._set("binding", _json(self.binding))
                    self.db.executemany("INSERT INTO frontier(url,depth) VALUES (?,0)",
                                        [(url,) for url in self.seeds])
                self.db.execute("UPDATE frontier SET status='PENDING' WHERE status='INFLIGHT'")
                self.db.execute("UPDATE attempts SET outcome='interrupted',finished=? "
                                "WHERE outcome='INFLIGHT'", (self.clock(),))
                self._set("status", "RUNNING")
                self._set("pid", str(os.getpid()))
            return self
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.lock.__exit__(*sys.exc_info())
            raise

    def __exit__(self, *exc):
        try:
            self.db.close()
        finally:
            self.lock.__exit__(*exc)

    def _set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def _request(self, url, interval):
        row = self.db.execute("SELECT value FROM meta WHERE key='last_request'").fetchone()
        previous = self.db.execute("SELECT value FROM meta WHERE key='last_interval'").fetchone()
        interval = max(interval, self.config.delay_seconds,
                       float(previous[0]) if previous else 0)
        if row:
            wait = float(row[0]) + interval - self.clock()
            if wait > 0:
                self.sleep(wait)
        # Commit before sending: a killed/failed request still consumes its spacing.
        with self.db:
            self._set("last_request", str(self.clock()))
            self._set("last_interval", str(interval))
            self.db.execute("INSERT INTO meta(key,value) VALUES ('requests','1') "
                            "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
        return self.fetch(url, self.config)

    def _allowed(self, url):
        if self.robots_callback:
            answer = self.robots_callback(url)
            if isinstance(answer, tuple):
                allowed, interval = answer
                if not math.isfinite(interval) or interval < 0:
                    raise CrawlError("invalid robots interval")
                return bool(allowed), interval
            return bool(answer), self.config.delay_seconds
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots_cache:
            parser = RobotFileParser()
            try:
                # Reject redirecting robots rather than risking ungoverned requests.
                _, body = self._request(origin + "/robots.txt", self.config.delay_seconds)
                parser.parse(body.splitlines())
                delay = parser.crawl_delay(self.config.user_agent) or 0
                rate = parser.request_rate(self.config.user_agent)
                interval = max(delay, rate.seconds / rate.requests if rate else 0,
                               self.config.delay_seconds)
                self.robots_cache[origin] = (parser, interval)
            except Exception as error:
                raise RobotsUnavailable("robots_unavailable") from error
        parser, interval = self.robots_cache[origin]
        return parser is not None and parser.can_fetch(self.config.user_agent, url), interval

    def _page(self, url):
        current = url
        for _ in range(6):
            allowed, interval = self._allowed(current)
            if not allowed:
                raise CrawlError("robots_denied")
            try:
                kind, body = self._request(current, interval)
                return current, kind, body
            except _Redirect as redirect:
                target = normalize_url(urljoin(current, redirect.target))
                if urlsplit(target).netloc != urlsplit(url).netloc:
                    raise CrawlError("cross_domain_redirect")
                current = target
        raise CrawlError("redirect_limit")

    def _parse_page(self, final_url, body):
        parser = _PageParser()
        parser.feed(body)
        parser.close()
        title, text, links = parser.parse(final_url)
        if not text:
            raise CrawlError("empty_text")
        return final_url, title, text, links

    def _store_page(self, parsed, pages):
        final_url, title, text, links = parsed
        if self.db.execute("SELECT 1 FROM pages WHERE url=?", (final_url,)).fetchone():
            return "duplicate"
        seq = pages + 1
        self.db.execute("INSERT INTO pages VALUES (?,?,?,?,?,?)",
                        (seq, f"crawl-{seq:06d}", final_url, title or final_url, text, _json(links)))
        return "success"

    def _record_failure(self, url, outcome, attempt):
        """Optional subclass evidence hook, called inside the completion transaction."""

    def run(self):
        try:
            while True:
                pages = self.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
                attempts = self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
                row = self.db.execute("SELECT url,depth FROM frontier WHERE status='PENDING' "
                                      "ORDER BY seq LIMIT 1").fetchone()
                if pages >= self.config.max_documents:
                    status = "TARGET_REACHED"
                    break
                if row is None:
                    status = "FRONTIER_EXHAUSTED"
                    break
                if attempts >= self.max_attempts:
                    status = "ATTEMPT_LIMIT"
                    break
                url, depth = row
                with self.db:
                    self.db.execute("UPDATE frontier SET status='INFLIGHT' WHERE url=?", (url,))
                    attempt = self.db.execute("INSERT INTO attempts(url,started,outcome) "
                                              "VALUES (?,?,'INFLIGHT')", (url, self.clock())).lastrowid
                outcome = "success"
                parsed = None
                try:
                    final_url, kind, body = self._page(url)
                    if kind and kind.lower() not in ("text/html", "application/xhtml+xml"):
                        raise CrawlError("non_html")
                    parsed = self._parse_page(final_url, body)
                except RobotsUnavailable:
                    raise
                except Exception as error:
                    # Store reason classes, never exception messages or response headers.
                    outcome = str(error) if isinstance(error, CrawlError) else type(error).__name__
                with self.db:
                    if parsed:
                        final_url, title, text, links = parsed
                        outcome = self._store_page(parsed, pages)
                        if depth < self.config.max_depth:
                            self.db.executemany("INSERT OR IGNORE INTO frontier(url,depth) VALUES (?,?)",
                                                [(link, depth + 1) for link in links
                                                 if urlsplit(link).netloc in self.hosts])
                    else:
                        self._record_failure(url, outcome, attempt)
                    self.db.execute("UPDATE frontier SET status='DONE' WHERE url=?", (url,))
                    self.db.execute("UPDATE attempts SET outcome=?,finished=? WHERE id=?",
                                    (outcome, self.clock(), attempt))
                if (attempts + 1) % self.export_every == 0:
                    self.export()
            with self.db:
                self._set("status", status)
        except BaseException:
            with self.db:
                self._set("status", "INTERRUPTED")
            self.export()
            raise
        return self.export()

    def _prepare_receipt(self, receipt):
        """Let subclasses publish required artifacts and add receipt fields."""

    def export(self):
        corpus = self.directory / "corpus.jsonl"
        temporary = self.directory / "corpus.jsonl.tmp"
        digest = hashlib.sha256()
        with temporary.open("wb") as handle:
            for doc_id, url, title, text in self.db.execute(
                    "SELECT doc_id,url,title,text FROM pages ORDER BY seq"):
                data = (_json(dict(doc_id=doc_id, url=url, title=title, text=text)) + "\n").encode("utf-8")
                handle.write(data)
                digest.update(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, corpus)
        meta = dict(self.db.execute("SELECT key,value FROM meta"))
        receipt = dict(status=meta["status"], documents=self.db.execute(
            "SELECT COUNT(*) FROM pages").fetchone()[0], attempts=self.db.execute(
            "SELECT COUNT(*) FROM attempts").fetchone()[0], requests=int(meta.get("requests", 0)),
            pending=self.db.execute("SELECT COUNT(*) FROM frontier WHERE status!='DONE'").fetchone()[0],
            outcomes=dict(self.db.execute("SELECT outcome,COUNT(*) FROM attempts GROUP BY outcome")),
            binding=self.binding, corpus_sha256=digest.hexdigest(), pid=int(meta["pid"]),
            exported_at=self.clock())
        self._prepare_receipt(receipt)
        temporary = self.directory / "receipt.json.tmp"
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(_json(receipt) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.directory / "receipt.json")
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--seed", action="append", required=True)
    parser.add_argument("--target-documents", type=int, default=20000)
    parser.add_argument("--max-depth", type=int, default=6)
    parser.add_argument("--max-attempts", type=int, default=100000)
    parser.add_argument("--delay-seconds", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("--max-bytes", type=int, default=1000000)
    parser.add_argument("--user-agent", default=CrawlConfig().user_agent)
    args = parser.parse_args(argv)
    previous_handler = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, terminate)
    try:
        config = CrawlConfig(max_documents=args.target_documents, max_depth=args.max_depth,
                             max_bytes=args.max_bytes, timeout=args.timeout,
                             delay_seconds=args.delay_seconds, user_agent=args.user_agent)
        with DurableCrawl(args.state_dir, args.seed, config, max_attempts=args.max_attempts) as crawl:
            print(_json(crawl.run()))
    except KeyboardInterrupt:
        return 130
    except (CrawlError, OSError, sqlite3.Error) as error:
        print(f"crawl error: {error}", file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
