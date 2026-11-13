"""Offline synthetic contracts; never contains captured publisher article text."""
import hashlib
import unittest

from evaluation.clean_news import (TextSignature, extract_article, is_near_duplicate,
                                   normalize_text, signature, simhash64)

URL = "https://www.news.cn/tech/20261101/synthetic/c.html"
PARA = "本地研究团队介绍了新的公共服务方案，项目将持续改善社区生活质量，并向公众提供完整信息。"
BODY = PARA * 4


def fixture(body=BODY, wrapper="span", selector="detailContent", title="合成新闻标题-新华网"):
    return f'<html><head><title>{title}</title><meta name="publishdate" content="2026-11-01"></head><body><nav>站点导航</nav><{wrapper} id="{selector}">{body}</{wrapper}><footer>其他内容</footer></body></html>'


class ExtractionTests(unittest.TestCase):
    def test_three_templates(self):
        for tag, selector in [("span", "detailContent"), ("div", "detailContent"), ("div", "detail")]:
            with self.subTest(tag=tag, selector=selector):
                result = extract_article(fixture(f'<p>{BODY}</p>', tag, selector), URL)
                self.assertTrue(result.accepted, result.metadata.filter_reasons)
                self.assertEqual(result.text, BODY)
                self.assertEqual(result.metadata.body_selector, '#' + selector)
                self.assertEqual(result.title, "合成新闻标题")
                self.assertEqual(result.metadata.published_at, "2026-11-01")

    def test_prefer_detail_content(self):
        result = extract_article(fixture(f'外层噪声<span id="detailContent"><p>{BODY}</p></span>尾部噪声', selector="detail"), URL)
        self.assertTrue(result.accepted)
        self.assertEqual(result.text, BODY)

    def test_nested_controls_and_scripts_excluded(self):
        controls = ('<script>evil("noise")</script><style>.x{}</style>'
                    '<noscript><b>禁用提示</b></noscript><template><div>模板噪声</div></template>'
                    '<svg><text>图形噪声</text></svg><iframe>框架噪声</iframe>'
                    '<div id="articleEdit"><div><span class="editor">责任编辑噪声</span></div></div>'
                    '<div class="relatedNews">相关推荐</div><div class="nextpage">下一篇</div>'
                    '<div class="share">分享控件</div><nav>导航</nav><button>按钮</button>')
        result = extract_article(fixture(f'<p>{BODY}</p>{controls}'), URL)
        self.assertTrue(result.accepted)
        self.assertEqual(result.text, BODY)

    def test_no_whole_page_fallback(self):
        result = extract_article(f'<title>导航-新华网</title><nav>{BODY}</nav>', URL)
        self.assertFalse(result.accepted)
        self.assertEqual(result.text, "")
        self.assertIn("unrecognized_body_template", result.metadata.filter_reasons)

    def test_hidden_and_template_body_ids_cannot_be_selected(self):
        for outer in ['template', 'noscript', 'nav']:
            html = f'<title>合成标题</title><{outer}><div id="detailContent">{BODY}</div></{outer}>'
            self.assertIn("unrecognized_body_template", extract_article(html, URL).metadata.filter_reasons)
        html = f'<title>合成标题</title><div hidden><div id="detailContent">{BODY}</div></div>'
        self.assertIn("unrecognized_body_template", extract_article(html, URL).metadata.filter_reasons)

    def test_quality_rejections(self):
        for body, reason in [("新闻短句", "body_too_short"), ("ascii " * 90, "insufficient_cjk"),
                             (BODY + "�" * 10, "replacement_character_ratio")]:
            result = extract_article(fixture(body), URL)
            self.assertFalse(result.accepted)
            self.assertIn(reason, result.metadata.filter_reasons)
        self.assertIn("empty_title", extract_article(fixture(title="-新华网"), URL).metadata.filter_reasons)

    def test_error_title_only(self):
        for title in ["404-新华网", "用户登录-新华网", "安全验证-新华网"]:
            self.assertIn("error_or_login_template", extract_article(fixture(title=title), URL).metadata.filter_reasons)
        result = extract_article(fixture(BODY + "报道讨论登录服务中的验证码设计。", title="登录与验证码服务改造新闻-新华网"), URL)
        self.assertTrue(result.accepted)

    def test_entities_unicode_paragraphs_and_hashes(self):
        body = f'<p>{PARA}&amp;ＡＢＣ&nbsp;测试&#x4e2d;</p><p>{BODY}</p>'
        html = fixture(body)
        result = extract_article(html, URL)
        self.assertTrue(result.accepted)
        self.assertEqual(result.text, PARA + '&ＡＢＣ 测试中\n' + BODY)
        self.assertEqual(result.metadata.raw_html_utf8_sha256, hashlib.sha256(html.encode()).hexdigest())
        self.assertEqual(result.metadata.cleaned_body_sha256, hashlib.sha256(result.text.encode()).hexdigest())
        self.assertIn("ＡＢＣ", result.text)
        self.assertEqual(result.metadata.normalized_text_sha256, signature(result.text).normalized_text_sha256)

    def test_url_boundary(self):
        for url in ['https://user:password@www.news.cn/a/c.html', 'https://news.cn/a/c.html',
                    'https://www.news.cn.evil.org/a/c.html', 'https://www.news.cn/a/index.html',
                    'file:///tmp/c.html', 'https://www.news.cn:8443/a/c.html']:
            with self.subTest(url=url):
                result = extract_article(fixture(), url)
                self.assertFalse(result.accepted)
                self.assertIsNone(result.metadata.source_url)
        result = extract_article(fixture(), URL + '#fragment')
        self.assertTrue(result.accepted)
        self.assertEqual(result.metadata.source_url, URL)

    def test_precise_title_suffix_and_unknown_date(self):
        result = extract_article(fixture(title="新华网专题中的合成报道"), URL)
        self.assertEqual(result.title, "新华网专题中的合成报道")
        self.assertIsNone(extract_article(fixture().replace('2026-11-01', 'unknown'), URL).metadata.published_at)


class DedupTests(unittest.TestCase):
    def test_nfkc_whitespace_exact_key(self):
        left, right = signature("ＡＢＣ　中国\n新闻"), signature("ABC 中国 新闻")
        self.assertEqual(left, right)
        self.assertEqual(normalize_text("ＡＢＣ　中国\n新闻"), "ABC 中国 新闻")

    def test_simhash_fixed_vector(self):
        expected = int.from_bytes(hashlib.sha256(b"alpha").digest()[:8], "big")
        self.assertEqual(simhash64("alpha alpha"), expected)
        self.assertEqual(simhash64(""), 0)
        self.assertEqual(simhash64(BODY), simhash64(BODY))

    def test_realistic_minor_edit_and_different_story(self):
        self.assertTrue(is_near_duplicate(signature(BODY), signature(BODY + "。")))
        self.assertFalse(is_near_duplicate(signature(BODY), signature("经济金融市场股票证券投资银行货币" * 20)))

    def test_hamming_and_length_boundaries(self):
        base = TextSignature("unused", 0, 100)
        self.assertTrue(is_near_duplicate(base, TextSignature("unused", 7, 90)))
        self.assertFalse(is_near_duplicate(base, TextSignature("unused", 15, 100)))
        self.assertFalse(is_near_duplicate(base, TextSignature("unused", 7, 89)))
        self.assertFalse(is_near_duplicate(signature(""), signature("")))


if __name__ == '__main__':
    unittest.main()
