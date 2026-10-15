"""分块：把长文本切成可检索、可引用的片段。

为什么要自己切、而不是按固定字数硬切：

- 结构优先（Markdown 标题、空行分段）能保住语义边界，
  检索出来的片段才是"一段完整的话"，而不是半句话；
- 段落超长时再退回滑动窗口 + overlap，避免单块过大挤爆上下文预算。

长度按**字符数**控制，不引入 tokenizer 依赖（中文一个字≈一个字符，
对预算估算已经够用；真正的 token 预算由注入层兜底）。
"""

import re


MAX_CHARS = 600
OVERLAP_CHARS = 80
MIN_TAIL_CHARS = 40

# Markdown 标题 / 空行都是结构性边界
HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
BLANK_LINE = re.compile(r"\n\s*\n+")
INLINE_SPACE = re.compile(r"[ \t\u3000]+")


def normalize(text: str) -> str:
    """统一换行与空白，让"同一内容"在哈希层面稳定。"""

    if not isinstance(text, str):
        raise ValueError("文本必须是字符串")

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = INLINE_SPACE.sub(" ", text)

    return "\n".join(
        line.rstrip() for line in text.split("\n")
    ).strip()


def split_blocks(text: str) -> list[str]:
    """先按标题切、再按空行切，得到结构块。"""

    blocks = []

    for section in HEADING.split(text):
        section = section.strip()

        if not section:
            continue

        for block in BLANK_LINE.split(section):
            block = block.strip()

            if block:
                blocks.append(block)

    return blocks


def split_long_block(block: str) -> list[str]:
    """超长块按窗口切分，相邻窗口保留 overlap。"""

    if len(block) <= MAX_CHARS:
        return [block]

    pieces = []
    start = 0

    while start < len(block):
        end = min(start + MAX_CHARS, len(block))
        pieces.append(block[start:end])

        if end >= len(block):
            break

        start = end - OVERLAP_CHARS

    return pieces


def chunk_text(text: str) -> list[str]:
    """把文本切成片段列表；空文本返回空列表。"""

    normalized = normalize(text)

    if not normalized:
        return []

    chunks = []

    for block in split_blocks(normalized):
        chunks.extend(split_long_block(block))

    # 合并过短的相邻片段：太碎会让检索命中"半句话"
    merged = []

    for chunk in chunks:
        if (
            merged
            and len(merged[-1]) < MIN_TAIL_CHARS
        ):
            merged[-1] = f"{merged[-1]}\n{chunk}"
        else:
            merged.append(chunk)

    return merged
