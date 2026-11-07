"""正文抽取：trafilatura 优先，BeautifulSoup 兜底，**失败显式**。

三条硬规则：

1. 抽不到就抛 `ExtractError`，绝不返回空字符串冒充"资料"。
2. 正文过短（默认 200 个非空白字符）视为失败 —— "抽到一点导航文字"
   和"抽到正文"必须能区分开。
3. 输出带 SHA256，供 Evidence Store 去重与对账。

解码是显式的一步：中文站点常见 GBK/GB18030，`requests` 给的
Content-Type 经常不带 charset，所以这里按 BOM → UTF-8 → meta charset
→ charset_normalizer 的顺序降级。
"""

from __future__ import annotations

import hashlib
import re

from dataclasses import dataclass

import trafilatura

from bs4 import BeautifulSoup


DEFAULT_MIN_CHARS = 200

# 这些容器里的文字不是正文，兜底路径里直接扔掉。
_BOILERPLATE_TAGS = (
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "svg",
    "iframe",
)

_CONTENT_TAGS = (
    "h1",
    "h2",
    "h3",
    "p",
    "li",
    "blockquote",
    "pre",
    "td",
)

_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset=["']?\s*([a-zA-Z0-9_\-]+)""",
    re.IGNORECASE,
)


class ExtractError(RuntimeError):
    """抽取失败：没有正文，或正文太短。"""


@dataclass(frozen=True)
class ExtractedText:
    """一次抽取的结果。`text` 已是可直接引用的正文。"""

    text: str
    title: str
    method: str
    sha256: str

    @property
    def char_count(self) -> int:
        return non_space_length(self.text)


def non_space_length(text: str) -> int:
    """非空白字符数 —— 判"正文够不够"的统一口径。"""

    return len("".join(text.split()))


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def decode_html(body: bytes) -> str:
    """把响应字节解码成文本，尽量不引入替换字符。"""

    if body.startswith(b"\xef\xbb\xbf"):
        return body.decode("utf-8-sig", "replace")

    try:
        return body.decode("utf-8")

    except UnicodeDecodeError:
        pass

    match = _META_CHARSET.search(body[:4096])

    if match is not None:
        encoding = match.group(1).decode("ascii", "ignore")

        try:
            return body.decode(encoding, "replace")

        except (LookupError, UnicodeDecodeError):
            pass

    try:
        from charset_normalizer import from_bytes

        best = from_bytes(body).best()

        if best is not None:
            return str(best)

    except Exception:  # noqa: BLE001 - 检测器本身失败不该阻断抽取
        pass

    return body.decode("utf-8", "replace")


def _clean_lines(text: str) -> str:
    """逐行压空白、去空行，保留段落边界。"""

    lines = [" ".join(line.split()) for line in text.splitlines()]
    kept = [line for line in lines if line]

    return "\n".join(kept)


def extract_title(html_text: str) -> str:
    try:
        soup = BeautifulSoup(html_text, "lxml")

    except Exception:  # noqa: BLE001
        return ""

    if soup.title is not None and soup.title.string:
        return " ".join(soup.title.string.split())[:200]

    heading = soup.find("h1")

    if heading is not None:
        return " ".join(heading.get_text(" ", strip=True).split())[:200]

    return ""


def _extract_with_trafilatura(html_text: str, url: str) -> str:
    try:
        result = trafilatura.extract(
            html_text,
            url=url or None,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )

    except Exception:  # noqa: BLE001 - 第三方库内部异常一律降级

        return ""

    if not isinstance(result, str):
        return ""

    return _clean_lines(result)


def _extract_with_bs4(html_text: str) -> str:
    try:
        soup = BeautifulSoup(html_text, "lxml")

    except Exception:  # noqa: BLE001
        return ""

    for tag in soup(_BOILERPLATE_TAGS):
        tag.decompose()

    blocks = []

    for tag in soup.find_all(_CONTENT_TAGS):
        text = " ".join(tag.get_text(" ", strip=True).split())

        if text:
            blocks.append(text)

    return _clean_lines("\n".join(blocks))


def extract_text(
    body: bytes,
    *,
    url: str = "",
    min_chars: int = DEFAULT_MIN_CHARS,
) -> ExtractedText:
    """从 HTML 字节里抽正文。

    trafilatura 结果够长就用它；不够或为空，退回 BeautifulSoup；
    两者都不够长，抛 `ExtractError`（附实际字符数，便于分类失败）。
    """

    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("body 必须是 bytes")

    if not body:
        raise ExtractError("响应体为空")

    html_text = decode_html(bytes(body))
    title = extract_title(html_text)

    primary = _extract_with_trafilatura(html_text, url)

    if non_space_length(primary) >= min_chars:
        return ExtractedText(
            text=primary,
            title=title,
            method="trafilatura",
            sha256=text_sha256(primary),
        )

    fallback = _extract_with_bs4(html_text)

    if non_space_length(fallback) >= min_chars:
        return ExtractedText(
            text=fallback,
            title=title,
            method="bs4",
            sha256=text_sha256(fallback),
        )

    raise ExtractError(
        "正文过短："
        f"trafilatura {non_space_length(primary)} 字符 / "
        f"bs4 {non_space_length(fallback)} 字符，"
        f"门槛 {min_chars}"
    )
