"""正文抽取单元测试。

用静态 HTML 夹具，不联网。
"""

import unittest

from pathlib import Path

from src.web.extract import (
    ExtractError,
    decode_html,
    extract_text,
    non_space_length,
    text_sha256,
)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "web"


def load(name):
    return (FIXTURES / name).read_bytes()


class TestExtractText(unittest.TestCase):
    """正常页面、短页面、无正文页面三类。"""

    def test_arxiv_like_page_yields_abstract(self):
        result = extract_text(
            load("page_arxiv.html"),
            url="https://arxiv.org/abs/2309.06180",
            min_chars=200,
        )

        self.assertGreaterEqual(result.char_count, 500)
        self.assertIn("PagedAttention", result.text)
        self.assertIn("Efficient Memory Management", result.title)
        self.assertIn(result.method, {"trafilatura", "bs4"})
        self.assertEqual(result.sha256, text_sha256(result.text))

    def test_navigation_and_footer_are_not_the_body(self):
        result = extract_text(load("page_arxiv.html"), min_chars=200)

        # 页头/页脚的样板文字不该占据正文主体
        self.assertNotIn("Skip to main content", result.text)
        self.assertLess(result.text.count("Privacy Policy"), 3)
        self.assertGreater(result.char_count, 500)

    def test_short_page_is_a_failure_not_an_empty_success(self):
        with self.assertRaises(ExtractError) as caught:
            extract_text(load("page_short.html"), min_chars=500)

        self.assertIn("正文过短", str(caught.exception))

    def test_client_rendered_page_has_no_body(self):
        with self.assertRaises(ExtractError):
            extract_text(load("page_broken.html"), min_chars=200)

    def test_empty_body_is_rejected(self):
        with self.assertRaises(ExtractError):
            extract_text(b"")

    def test_non_bytes_input_is_rejected(self):
        with self.assertRaises(TypeError):
            extract_text("<html>text</html>")


class TestDecoding(unittest.TestCase):
    """中文站点的编码降级。"""

    def test_gb18030_page_decodes_from_meta_charset(self):
        body = (
            '<html><head><meta charset="gb18030"></head>'
            "<body><p>中文正文用来验证解码路径是否正确。</p></body></html>"
        ).encode("gb18030", "replace")

        # 先用严格 UTF-8 解码会失败，必须靠 meta charset 兜住
        text = decode_html(body)

        self.assertIn("中文正文", text)

    def test_utf8_bom_is_stripped(self):
        text = decode_html("你好".encode("utf-8-sig"))

        self.assertTrue(text.startswith("你好"))


class TestCharCounting(unittest.TestCase):
    """字符口径：非空白字符数。"""

    def test_whitespace_does_not_count(self):
        self.assertEqual(non_space_length(" a b\n\tc "), 3)
        self.assertEqual(non_space_length("   \n\t "), 0)
        self.assertEqual(non_space_length("中文 中文"), 4)


if __name__ == "__main__":
    unittest.main()
