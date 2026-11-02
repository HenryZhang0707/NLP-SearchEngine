"""Offline clean-collection integration, using synthetic publisher-shaped HTML."""
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from evaluation.crawl_articles import CleanArticleCrawl
from evaluation.crawl_web import CrawlConfig, CrawlError
from evaluation.test_crawl_resume import Clock

ROOT = 'https://www.news.cn/'
A = ROOT + 'tech/20261101/a/c.html'
B = ROOT + 'tech/20261101/b/c.html'
C = ROOT + 'tech/20261101/c/c.html'
D = ROOT + 'tech/20261101/d/c.html'
TEXT = '本地研究团队介绍了新的公共服务方案，项目将持续改善社区生活质量，并向公众提供完整信息。' * 4
OTHER = '经济金融市场股票证券投资银行货币政策观察报告公布最新产业分析成果。' * 5


def article(body=TEXT, links=''):
    return f'<title>合成新闻-新华网</title><div id="detailContent"><p>{body}</p></div>{links}'


def response(text):
    return 'text/html', 'utf-8', text.encode('utf-8')


class CleanCrawlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.clock = Clock()
        self.config = CrawlConfig(max_documents=20, max_depth=3, delay_seconds=.5)

    def tearDown(self):
        self.temp.cleanup()

    def make(self, fetch, **kwargs):
        options = dict(config=self.config, fetch=fetch, robots=lambda url: True,
                       clock=self.clock.now, sleep=self.clock.sleep, export_every=1)
        options.update(kwargs)
        seeds = options.pop('seeds', [ROOT])
        return CleanArticleCrawl(self.path, seeds, **options)

    def test_navigation_exact_near_and_body_cache_hashes(self):
        pages = {ROOT: f'<title>导航</title><a href="{A}">新闻</a>',
                 A: article(links=f'<a href="{B}">重复</a>'),
                 B: article(links=f'<a href="{C}">近重复</a>'),
                 C: article(TEXT + '。', f'<a href="{D}">其他</a>'), D: article(OTHER)}
        with self.make(lambda u, c: response(pages[u]), config=CrawlConfig(max_documents=20, max_depth=4)) as crawler:
            receipt = crawler.run()
            self.assertEqual(receipt['documents'], 2)
            self.assertEqual(receipt['outcomes'], {'duplicate_exact': 1, 'duplicate_near': 1, 'filtered': 1, 'success': 2})
            self.assertEqual(crawler.db.execute('SELECT COUNT(*) FROM signatures').fetchone()[0], 2)
            self.assertEqual(crawler.db.execute('SELECT representative_seq FROM evidence WHERE outcome LIKE "duplicate_%" ORDER BY attempt').fetchall(), [(1,), (1,)])
        corpus = [json.loads(line) for line in (self.path / 'corpus.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['doc_id'] for r in corpus], ['crawl-000001', 'crawl-000002'])
        self.assertEqual(corpus[0]['text'], TEXT)
        self.assertNotIn('导航', corpus[0]['text'])
        sidecar_raw = (self.path / 'article_sidecar.jsonl').read_bytes()
        self.assertEqual(hashlib.sha256(sidecar_raw).hexdigest(), receipt['sidecar_sha256'])
        rows = [json.loads(line) for line in sidecar_raw.splitlines()]
        capture = rows[1]['captures'][0]
        original = pages[A].encode()
        self.assertEqual(capture['http_bytes_sha256'], hashlib.sha256(original).hexdigest())
        cached = (self.path / capture['cache_path']).read_bytes()
        self.assertEqual(cached[4:8], b'\0\0\0\0')
        self.assertEqual(gzip.decompress(cached), original)
        self.assertEqual(rows[1]['clean_metadata']['raw_html_utf8_sha256'], hashlib.sha256(original).hexdigest())
        self.assertNotEqual(rows[1]['clean_metadata']['cleaned_body_sha256'], capture['http_bytes_sha256'])

    def test_http_and_decoded_hash_distinct(self):
        raw = article().encode('gb18030')
        with self.make(lambda u, c: ('text/html', 'gb18030', raw), seeds=[A]) as crawler:
            crawler.run()
        row = json.loads((self.path/'article_sidecar.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.assertNotEqual(row['captures'][0]['http_bytes_sha256'], row['clean_metadata']['raw_html_utf8_sha256'])

    def test_interrupt_resume_stable_attempt_budget(self):
        pages = {ROOT: f'<a href="{A}">A</a><a href="{D}">D</a>', A: article(), D: article(OTHER)}
        def first(url, config):
            if url == D:
                raise KeyboardInterrupt()
            return response(pages[url])
        with self.make(first, max_attempts=4) as crawler:
            with self.assertRaises(KeyboardInterrupt):
                crawler.run()
            before = (self.path / 'corpus.jsonl').read_bytes()
        with self.make(lambda u,c: response(pages[u]), max_attempts=4) as crawler:
            receipt = crawler.run()
            self.assertEqual(receipt['attempts'], 4)
            self.assertEqual(receipt['documents'], 2)
            self.assertEqual(receipt['outcomes']['interrupted'], 1)
        self.assertTrue((self.path / 'corpus.jsonl').read_bytes().startswith(before))
        with self.make(lambda u,c: self.fail('must not refetch'), max_attempts=4) as crawler:
            self.assertEqual(crawler.run()['attempts'], 4)

    def test_configuration_code_rule_seed_bindings(self):
        with self.make(lambda u,c: response('navigation')):
            pass
        for key in ['rule_version', 'clean_source_sha256', 'collection_source_sha256', 'analyzer_config_hash', 'seed_file_sha256']:
            changed = self.make(None)
            changed.binding[key] = 'changed'
            with self.assertRaisesRegex(CrawlError, 'configuration mismatch'):
                with changed:
                    pass
        with self.assertRaisesRegex(CrawlError, 'configuration mismatch'):
            with self.make(None, config=CrawlConfig(max_documents=2)):
                pass

    def test_target_counts_accepted_and_filter_links(self):
        pages = {ROOT: f'<a href="{A}">A</a>', A: article('短', f'<a href="{D}">D</a>'), D: article()}
        with self.make(lambda u,c: response(pages[u]), config=CrawlConfig(max_documents=1, max_depth=3)) as crawler:
            receipt = crawler.run()
            self.assertEqual(receipt['status'], 'TARGET_REACHED')
            self.assertEqual(receipt['attempts'], 3)
            self.assertEqual(receipt['accepted_unique_documents'], 1)

    def test_attempt_limit_does_not_treat_filtered_as_documents(self):
        pages = {ROOT: f'<a href="{A}">A</a>', A: article()}
        with self.make(lambda u,c: response(pages[u]), max_attempts=1) as crawler:
            receipt = crawler.run()
            self.assertEqual(receipt['status'], 'ATTEMPT_LIMIT')
            self.assertEqual(receipt['documents'], 0)
        with self.make(lambda u,c: self.fail('budget must remain spent'), max_attempts=1) as crawler:
            self.assertEqual(crawler.run()['attempts'], 1)

    def test_interrupt_after_cache_preserves_capture_and_retries_new_attempt(self):
        with self.make(lambda u,c: response(article()), seeds=[A]) as crawler:
            def interrupt(url, body):
                raise KeyboardInterrupt()
            crawler._parse_page = interrupt
            with self.assertRaises(KeyboardInterrupt):
                crawler.run()
            self.assertEqual(crawler.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 1)
            self.assertEqual(crawler.db.execute('SELECT COUNT(*) FROM pages').fetchone()[0], 0)
        with self.make(lambda u,c: response(article()), seeds=[A]) as crawler:
            receipt = crawler.run()
            self.assertEqual(receipt['documents'], 1)
            self.assertEqual(receipt['attempts'], 2)
            self.assertEqual(receipt['raw_capture_count'], 2)

    def test_credentials_rejected_without_sidecar_leak(self):
        html = article(links='<a href="https://user:TOPSECRET@www.news.cn/x/c.html">unsafe</a>')
        with self.make(lambda u,c: response(html)) as crawler:
            crawler.run()
        self.assertNotIn('TOPSECRET', (self.path/'article_sidecar.jsonl').read_text(encoding='utf-8'))
        with self.assertRaises(CrawlError):
            CleanArticleCrawl(self.path, ['https://user:TOPSECRET@www.news.cn/'])

    def test_non_html_failure_preserves_capture(self):
        with self.make(lambda u,c: ('application/json', 'utf-8', b'{}')) as crawler:
            receipt = crawler.run()
            self.assertEqual(receipt['outcomes'], {'non_html': 1})
        row = json.loads((self.path/'article_sidecar.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(len(row['captures']), 1)
        self.assertIsNone(row['clean_metadata'])


if __name__ == '__main__':
    unittest.main()
