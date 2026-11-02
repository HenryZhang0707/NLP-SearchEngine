"""Offline durability and request-governance checks."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from evaluation.crawl_resume import DurableCrawl, RobotsUnavailable, StateLocked, _Redirect, exclusive_lock
from evaluation.crawl_web import CrawlConfig, CrawlError


class Clock:
    def __init__(self):
        self.value = 1000.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.clock = Clock()
        self.requests = []
        self.config = CrawlConfig(max_documents=20, max_depth=2, delay_seconds=.5)

    def tearDown(self):
        self.temp.cleanup()

    def make(self, fetch, **kwargs):
        options = dict(config=self.config, fetch=fetch, robots=lambda url: True,
                       clock=self.clock.now, sleep=self.clock.sleep)
        options.update(kwargs)
        return DurableCrawl(self.path, ["https://a.test/"], **options)

    def test_interrupt_resume_stable_ids_and_attempts(self):
        bodies = {"https://a.test/": '<title>root</title><a href="/x">X</a><a href="/y">Y</a>',
                  "https://a.test/x": 'x', "https://a.test/y": 'y'}
        def first(url, config):
            if url.endswith('/x'):
                raise KeyboardInterrupt()
            return 'text/html', bodies[url]
        with self.make(first) as crawler:
            with self.assertRaises(KeyboardInterrupt):
                crawler.run()
            saved = (self.path / 'corpus.jsonl').read_bytes()
            self.assertEqual(crawler.export()['status'], 'INTERRUPTED')
        with self.make(lambda u, c: ('text/html', bodies[u])) as crawler:
            receipt = crawler.run()
            self.assertEqual(receipt['attempts'], 4)
            self.assertEqual(receipt['outcomes']['interrupted'], 1)
            self.assertEqual(receipt['documents'], 3)
            self.assertEqual(receipt['status'], 'FRONTIER_EXHAUSTED')
        data = (self.path / 'corpus.jsonl').read_bytes()
        self.assertTrue(data.startswith(saved))
        rows = [json.loads(line) for line in data.splitlines()]
        self.assertEqual([r['doc_id'] for r in rows], ['crawl-000001', 'crawl-000002', 'crawl-000003'])
        self.assertEqual(hashlib.sha256(data).hexdigest(), receipt['corpus_sha256'])
        with self.make(lambda u, c: self.fail('completed crawl refetched')) as crawler:
            self.assertEqual(crawler.run()['corpus_sha256'], receipt['corpus_sha256'])

    def test_configuration_mismatch_and_lock_release(self):
        with self.make(lambda u, c: ('text/html', 'hi')):
            with self.assertRaises(StateLocked):
                with exclusive_lock(self.path):
                    pass
        with self.assertRaisesRegex(CrawlError, 'configuration mismatch'):
            with self.make(None, config=CrawlConfig(max_documents=2)):
                pass
        with exclusive_lock(self.path):
            pass

    def test_lock_is_enforced_across_processes(self):
        script = ('import sys\n'
                  'from evaluation.crawl_resume import exclusive_lock, StateLocked\n'
                  'try:\n'
                  ' with exclusive_lock(sys.argv[1]): pass\n'
                  'except StateLocked: sys.exit(42)\n')
        with exclusive_lock(self.path):
            result = subprocess.run([sys.executable, '-c', script, str(self.path)],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 42, result.stderr.decode())
        result = subprocess.run([sys.executable, '-c', script, str(self.path)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_crawler_code_binding_mismatch(self):
        with self.make(lambda u, c: ('text/html', 'body')) as crawler:
            self.assertEqual(len(crawler.binding['crawler_source_sha256']), 64)
        changed = self.make(lambda u, c: ('text/html', 'body'))
        changed.binding['crawler_source_sha256'] = '0' * 64
        with self.assertRaisesRegex(CrawlError, 'configuration mismatch'):
            with changed:
                pass
        # Rejected entry releases its OS lock and preserves the authoritative DB.
        with self.make(lambda u, c: ('text/html', 'body')) as crawler:
            self.assertEqual(crawler.run()['documents'], 1)

    def test_hard_stop_inflight_recovered_without_budget_refund(self):
        with self.make(lambda u, c: ('text/html', 'body')) as crawler:
            with crawler.db:
                crawler.db.execute("UPDATE frontier SET status='INFLIGHT'")
                crawler.db.execute("INSERT INTO attempts(url,started,outcome) VALUES (?,?,'INFLIGHT')",
                                   ('https://a.test/', self.clock.now()))
        with self.make(lambda u, c: ('text/html', 'body')) as crawler:
            receipt = crawler.run()
        self.assertEqual(receipt['attempts'], 2)
        self.assertEqual(receipt['outcomes'], {'interrupted': 1, 'success': 1})

    def test_failed_and_leaf_requests_paced(self):
        def fetch(url, config):
            self.requests.append((url, self.clock.now()))
            if url.endswith('/bad'):
                raise OSError('offline')
            if url.endswith('/leaf'):
                return 'text/html', 'leaf'
            return 'text/html', '<a href="/bad">bad</a><a href="/leaf">leaf</a><a href="/last">last</a>'
        with self.make(fetch) as crawler:
            receipt = crawler.run()
        self.assertEqual(len(self.requests), 4)
        self.assertTrue(all(b[1]-a[1] >= .5 for a, b in zip(self.requests, self.requests[1:])))
        self.assertEqual(receipt['outcomes']['OSError'], 1)

    def test_budget_not_reset_by_interruption(self):
        def interrupt(url, config):
            raise KeyboardInterrupt()
        with self.make(interrupt, max_attempts=1) as crawler:
            with self.assertRaises(KeyboardInterrupt):
                crawler.run()
        with self.make(lambda u, c: self.fail('budget bypass'), max_attempts=1) as crawler:
            receipt = crawler.run()
        self.assertEqual((receipt['status'], receipt['attempts'], receipt['documents']),
                         ('ATTEMPT_LIMIT', 1, 0))

    def test_robots_denial_and_failure(self):
        with self.make(lambda u, c: self.fail('robots denied'), robots=lambda u: False) as crawler:
            receipt = crawler.run()
        self.assertEqual(receipt['outcomes'], {'robots_denied': 1})
        self.assertEqual(receipt['requests'], 0)

    def test_real_robots_path_rate_redirect_and_success_dedup(self):
        def fetch(url, config):
            self.requests.append((url, self.clock.now()))
            if url.endswith('/robots.txt'):
                return 'text/plain', 'User-agent: *\nDisallow: /deny\nCrawl-delay: 3\nRequest-rate: 1/5'
            if url.endswith('/alias'):
                raise _Redirect('/x')
            if url.endswith('/away'):
                raise _Redirect('https://outside.test/')
            if url.endswith('/x'):
                return 'text/html', 'target'
            return 'text/html', '<a href="/x">x</a><a href="/alias">alias</a><a href="/away">away</a><a href="/deny">deny</a><a href="https://outside.test/">offsite</a>'
        with self.make(fetch, robots=None) as crawler:
            receipt = crawler.run()
        self.assertEqual(receipt['documents'], 2)
        self.assertEqual(receipt['outcomes']['duplicate'], 1)
        self.assertEqual(receipt['outcomes']['cross_domain_redirect'], 1)
        self.assertEqual(receipt['outcomes']['robots_denied'], 1)
        self.assertTrue(all(b[1]-a[1] >= 5 for a, b in zip(self.requests, self.requests[1:])))
        self.assertFalse(any('outside' in u or u.endswith('/deny') for u, _ in self.requests))

    def test_robots_fetch_failure_is_closed(self):
        def fetch(url, config):
            self.assertTrue(url.endswith('/robots.txt'))
            raise OSError('offline')
        with self.make(fetch, robots=None) as crawler:
            with self.assertRaises(RobotsUnavailable):
                crawler.run()
            receipt = crawler.export()
        self.assertEqual(receipt['requests'], 1)
        self.assertEqual(receipt['status'], 'INTERRUPTED')
        self.assertEqual(receipt['outcomes'], {'INFLIGHT': 1})
        self.assertEqual(receipt['pending'], 1)
        def recovered(url, config):
            if url.endswith('/robots.txt'):
                return 'text/plain', 'User-agent: *\nAllow: /'
            return 'text/html', 'recovered page'
        with self.make(recovered, robots=None) as crawler:
            receipt = crawler.run()
        self.assertEqual(receipt['status'], 'FRONTIER_EXHAUSTED')
        self.assertEqual(receipt['attempts'], 2)
        self.assertEqual(receipt['outcomes'], {'interrupted': 1, 'success': 1})
        rows = [json.loads(line) for line in (self.path / 'corpus.jsonl').read_text().splitlines()]
        self.assertEqual([row['doc_id'] for row in rows], ['crawl-000001'])

    def test_robots_failure_resume_cannot_refund_attempt_budget(self):
        def failed(url, config):
            raise OSError('offline')
        with self.make(failed, robots=None, max_attempts=1) as crawler:
            with self.assertRaises(RobotsUnavailable):
                crawler.run()
        with self.make(lambda u, c: self.fail('attempt budget bypass'), robots=None,
                       max_attempts=1) as crawler:
            receipt = crawler.run()
        self.assertEqual(receipt['status'], 'ATTEMPT_LIMIT')
        self.assertEqual(receipt['attempts'], 1)
        self.assertEqual(receipt['documents'], 0)

    def test_target_and_unsafe_urls(self):
        for seed in ('file:///x', 'https://user:pass@a.test/'):
            with self.assertRaises(CrawlError):
                DurableCrawl(self.path, [seed])
        with self.make(lambda u, c: ('text/html', '<a href="/x">x</a>'),
                       config=CrawlConfig(max_documents=1)) as crawler:
            receipt = crawler.run()
        self.assertEqual(receipt['status'], 'TARGET_REACHED')
        self.assertEqual(receipt['documents'], 1)


if __name__ == '__main__':
    unittest.main()
