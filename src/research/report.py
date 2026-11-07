"""报告渲染与引用对账。

两条分工明确的职责：

1. **引用对账（机器断言）**：正文里每个 `[E#]` 都必须能在 Evidence Store 里
   找到。找不到就 `ReportError` —— 报告宁可不出，也不许出现凭空的来源。
2. **参考来源附录（代码生成）**：标题、URL、SHA256 全部从证据库读，
   **不经模型**。模型只负责正文，来源清单它碰不到。

字数与来源数是**判据**，不是装饰：不足时返回 `passed=False` 并列出问题，
由调用方决定记 `partial`。**这里不会为了凑数把附录算进字数。**
"""

from __future__ import annotations

import re

from dataclasses import dataclass

from src.research.prompts import WriterDraft


MIN_CHARS = 3000
MIN_SOURCES = 10

_CITATION = re.compile(r"\[E(\d+)\]")


class ReportError(RuntimeError):
    """报告不成立（引用指向不存在的证据、结构非法）。"""


@dataclass(frozen=True)
class RenderedReport:
    markdown: str
    char_count: int
    source_count: int
    evidence_ids: tuple[int, ...]
    problems: tuple[str, ...] = ()
    missing_ids: tuple[int, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.problems and not self.missing_ids


def _clean(text: str) -> str:
    return " ".join(text.split())


def count_chars(text: str) -> int:
    """非空白字符数 —— 与抽取层同一个口径。"""

    return len("".join(text.split()))


def _collect_ids(draft: WriterDraft) -> list[int]:
    seen = []

    for section in draft.sections:
        for paragraph in section.paragraphs:
            for evidence_id in paragraph.evidence_ids:
                if evidence_id not in seen:
                    seen.append(evidence_id)

    return seen


def render_report(
    draft: WriterDraft,
    store,
    *,
    question: str = "",
    min_chars: int = MIN_CHARS,
    min_sources: int = MIN_SOURCES,
) -> RenderedReport:
    """把草稿渲染成 Markdown，并做引用对账与判据检查。"""

    if not isinstance(draft, WriterDraft):
        raise TypeError("draft 必须是 WriterDraft")

    for name, value in (("min_chars", min_chars), ("min_sources", min_sources)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} 必须是非负整数")

    ids = _collect_ids(draft)
    missing = [evidence_id for evidence_id in ids if store.get_source(evidence_id) is None]

    if missing:
        raise ReportError(
            "正文引用了不存在的证据：" + ", ".join(f"E{item}" for item in missing)
        )

    lines = [f"# {_clean(draft.title)}", ""]

    if question:
        lines += [f"> 研究问题：{_clean(question)}", ""]

    lines += [f"## 摘要", "", _clean(draft.summary), ""]

    for section in draft.sections:
        lines += [f"## {_clean(section.heading)}", ""]

        for paragraph in section.paragraphs:
            marks = "".join(f"[E{item}]" for item in paragraph.evidence_ids)
            lines += [f"{_clean(paragraph.text)} {marks}", ""]

    if draft.limitations:
        lines += ["## 局限与未解问题", "", _clean(draft.limitations), ""]

    body = "\n".join(lines)

    appendix = ["## 参考来源", ""]

    for evidence_id in ids:
        record = store.get_source(evidence_id)
        title = _clean(record.title) or "(无标题)"
        appendix.append(
            f"- [E{evidence_id}] {title} — {record.url}"
            f"（sha256: {record.text_sha256[:12]}）"
        )

    markdown = body + "\n".join(appendix) + "\n"

    char_count = count_chars(body)
    problems = []

    if char_count < min_chars:
        problems.append(f"字数不足：{char_count} < {min_chars}")

    if len(ids) < min_sources:
        problems.append(f"来源不足：{len(ids)} < {min_sources}")

    return RenderedReport(
        markdown=markdown,
        char_count=char_count,
        source_count=len(ids),
        evidence_ids=tuple(ids),
        problems=tuple(problems),
    )


def check_report(markdown: str, store) -> tuple[int, ...]:
    """机器断言：Markdown 里每个 `[E#]` 都要能命中证据库。

    返回**缺失的** id 元组（空元组 = 全部命中）。这是路线图 §9.4
    「每个 URL/编号都能命中 Evidence Store」的实现。
    """

    if not isinstance(markdown, str) or not markdown.strip():
        raise ReportError("报告为空")

    referenced = {int(match) for match in _CITATION.findall(markdown)}
    missing = [
        evidence_id
        for evidence_id in sorted(referenced)
        if store.get_source(evidence_id) is None
    ]

    return tuple(missing)


def summarize(report: RenderedReport) -> str:
    """一行摘要，给日志和证据包用。"""

    status = "PASS" if report.passed else "PARTIAL"

    return (
        f"{status} chars={report.char_count} sources={report.source_count} "
        f"problems={'; '.join(report.problems) or '-'}"
    )
