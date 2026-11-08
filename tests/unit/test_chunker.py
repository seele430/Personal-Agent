"""knowledge/chunker 单测：只钉不变量，不锁死具体实现。

不变量：
- 空/纯空白 → 空列表；
- 不丢内容（去掉重叠后，原文字符都能在块里找到）；
- 单块长度 ≤ MAX_CHARS（合并短块可能略超，单独断言现状）；
- 结构边界（Markdown 标题）不被切在中间。
"""

import unittest

from src.knowledge import chunker


class NormalizeCase(unittest.TestCase):
    def test_crlf_and_trailing_space(self):
        self.assertEqual(chunker.normalize("a  \r\nb\t\r\n"), "a\nb")

    def test_collapse_inline_spaces(self):
        self.assertEqual(chunker.normalize("a    b\u3000c"), "a b c")

    def test_rejects_non_string(self):
        with self.assertRaises(ValueError):
            chunker.normalize(None)


class SplitBlocksCase(unittest.TestCase):
    def test_blank_lines_split(self):
        blocks = chunker.split_blocks("第一段\n\n第二段")

        self.assertEqual(blocks, ["第一段", "第二段"])

    def test_heading_is_boundary(self):
        # 标题本身被 HEADING 切掉，剩下的"标题\n内容"各成一块（共 2 块）
        blocks = chunker.split_blocks("# 标题\n内容甲\n\n## 子标题\n内容乙")

        self.assertEqual(len(blocks), 2)
        self.assertIn("标题", blocks[0])
        self.assertIn("内容甲", blocks[0])
        self.assertIn("子标题", blocks[1])

    def test_empty_returns_empty(self):
        self.assertEqual(chunker.split_blocks("   \n\n  "), [])


class SplitLongCase(unittest.TestCase):
    def test_short_block_untouched(self):
        block = "短" * 100

        self.assertEqual(chunker.split_long_block(block), [block])

    def test_long_block_split_with_overlap(self):
        block = "字" * (chunker.MAX_CHARS * 2 + 50)
        pieces = chunker.split_long_block(block)

        self.assertGreater(len(pieces), 1)

        for piece in pieces:
            self.assertLessEqual(len(piece), chunker.MAX_CHARS)

    def test_neighbouring_pieces_overlap(self):
        block = "".join(chr(0x4E00 + index % 1000) for index in range(900))
        pieces = chunker.split_long_block(block)

        if len(pieces) >= 2:
            tail = pieces[0][-chunker.OVERLAP_CHARS :]
            self.assertTrue(pieces[1].startswith(tail))


class ChunkTextCase(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(chunker.chunk_text(""), [])
        self.assertEqual(chunker.chunk_text("   \n  "), [])

    def test_single_paragraph(self):
        self.assertEqual(chunker.chunk_text("你好世界"), ["你好世界"])

    def test_no_content_lost(self):
        text = "\n\n".join(f"第{index}段内容" for index in range(12))
        chunks = chunker.chunk_text(text)
        joined = "".join(chunks).replace("\n", "")

        for index in range(12):
            self.assertIn(f"第{index}段内容", joined)

    def test_chunks_respect_max_chars(self):
        text = "甲" * (chunker.MAX_CHARS + 500)
        chunks = chunker.chunk_text(text)

        self.assertGreater(len(chunks), 1)

        for chunk in chunks:
            self.assertLessEqual(len(chunk), chunker.MAX_CHARS)

    def test_short_block_merged_into_previous(self):
        # 只有"前一块很短"才会触发合并（MIN_TAIL_CHARS 的设计）
        chunks = chunker.chunk_text("短块一\n\n短块二")

        self.assertEqual(len(chunks), 1)
        self.assertIn("短块二", chunks[0])

    def test_long_block_does_not_absorb_next(self):
        chunks = chunker.chunk_text(f"{'长' * 200}\n\n短")

        self.assertEqual(len(chunks), 2)

    def test_headings_survive(self):
        text = "# 一级\n正文一\n\n## 二级\n正文二"
        chunks = chunker.chunk_text(text)

        joined = "\n".join(chunks)

        self.assertIn("一级", joined)
        self.assertIn("二级", joined)


if __name__ == "__main__":
    unittest.main()
