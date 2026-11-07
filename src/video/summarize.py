"""分段摘要：把转写切块、逐块提炼要点、再把时间戳**从原始段回填**。

关键约束：**模型不许自己写时间**。它只输出"这条要点引用了哪几段"（段索引），
起止时间由代码从转写里取。这样时间戳误差只可能来自分段粒度，不会来自幻觉。
"""

from __future__ import annotations

import re

from dataclasses import dataclass

from src.video.asr import Segment, Transcript


CHUNK_SEGMENTS = 8
MAX_CHUNKS = 30
MAX_POINTS = 20
MAX_POINT_CHARS = 160

_CITATION = re.compile(r"\[(\d+)(?:-(\d+))?\]")


class SummarizeError(RuntimeError):
    """摘要不可用（模型返回不合法）。"""


@dataclass(frozen=True)
class Point:
    text: str
    start: float
    end: float
    segment_indices: tuple[int, ...]

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "segments": list(self.segment_indices),
        }


def chunk_segments(
    segments: tuple[Segment, ...],
    *,
    size: int = CHUNK_SEGMENTS,
    max_chunks: int = MAX_CHUNKS,
) -> list[tuple[int, tuple[Segment, ...]]]:
    """按序号切块，返回 [(起始索引, 段元组)]；块数有上限。"""

    if size < 1:
        raise ValueError("size 必须 ≥1")

    chunks = []

    for offset in range(0, len(segments), size):
        chunks.append((offset, tuple(segments[offset : offset + size])))

        if len(chunks) >= max_chunks:
            break

    return chunks


def build_chunk_messages(
    chunk,
    *,
    index: int,
    total: int,
) -> list[dict]:
    offset, segments = chunk

    listing = "\n".join(
        f"[{offset + position}] {segment.text}"
        for position, segment in enumerate(segments)
    )

    return [
        {
            "role": "system",
            "content": (
                "你在给视频做结构化摘要。只输出 JSON，不要解释。\n"
                "每条要点必须标注它依据的段号（方括号里的整数），"
                "不要编造段号，也不要写时间。\n"
            ),
        },
        {
            "role": "user",
            "content": (
                f"这是第 {index + 1}/{total} 段转写：\n{listing}\n\n"
                '输出：{"points": [{"text": "要点", "segments": [12, 13]}]}'
            ),
        },
    ]


def _clean_point(text: str) -> str:
    cleaned = " ".join(str(text).split())

    return cleaned[:MAX_POINT_CHARS]


def points_from_payload(
    payload,
    *,
    chunk,
    segments: tuple[Segment, ...],
) -> list[Point]:
    """把模型返回的 points 映射成带时间戳的要点。"""

    if not isinstance(payload, dict):
        raise SummarizeError("摘要结果不是对象")

    raw_points = payload.get("points")

    if not isinstance(raw_points, list):
        raise SummarizeError("摘要结果缺少 points 数组")

    offset, _ = chunk
    points = []

    for item in raw_points:
        if not isinstance(item, dict):
            continue

        text = _clean_point(item.get("text") or "")

        if not text:
            continue

        raw_indices = item.get("segments")

        if not isinstance(raw_indices, list) or not raw_indices:
            # 没有段号 = 无法定位时间，直接丢弃（不猜时间）
            continue

        indices = []

        for value in raw_indices:
            if isinstance(value, bool) or not isinstance(value, int):
                continue

            if 0 <= value < len(segments) and value not in indices:
                indices.append(value)

        if not indices:
            continue

        chosen = [segments[index] for index in indices]

        points.append(
            Point(
                text=text,
                start=min(segment.start for segment in chosen),
                end=max(segment.end for segment in chosen),
                segment_indices=tuple(indices),
            )
        )

    return points


def summarize(
    transcript: Transcript,
    *,
    completer=None,
    max_points: int = MAX_POINTS,
    usage_sink=None,
    on_truncated=None,
) -> list[Point]:
    """逐块提炼要点，合并去重后返回（最多 max_points 条）。

    `usage_sink(usage)` 只对**真实 LLM 调用**触发；注入 completer 的测试路径
    不会回调，避免编造用量。

    `on_truncated(info)` 在块数超过上限时触发——**不再静默丢弃**长视频的后半段。
    """

    if not transcript.segments:
        raise SummarizeError("没有可摘要的转写")

    chunks = chunk_segments(transcript.segments)
    total_chunks = (len(transcript.segments) + CHUNK_SEGMENTS - 1) // CHUNK_SEGMENTS

    if on_truncated is not None and total_chunks > len(chunks):
        on_truncated({"total_chunks": total_chunks, "used_chunks": len(chunks)})
    points: list[Point] = []
    seen: set[str] = set()

    for index, chunk in enumerate(chunks):
        messages = build_chunk_messages(chunk, index=index, total=len(chunks))

        if completer is None:
            from src.research.llm import complete_json

            payload, call = complete_json(messages, max_tokens=1024, temperature=0.0)

            if usage_sink is not None:
                usage_sink(call.usage)

        else:
            payload = completer(messages, chunk)

        offset, _ = chunk
        local_chunk = (offset, transcript.segments[offset : offset + len(chunk[1])])

        for point in points_from_payload(
            payload, chunk=local_chunk, segments=transcript.segments
        ):
            key = point.text.lower()

            if key in seen:
                continue

            seen.add(key)
            points.append(point)

    # 不早退：所有块都要跑（否则长视频只摘要前半段）。
    # 超过上限时**两端锚定**等距抽样：首条与末条必留，中间均匀取。
    if len(points) > max_points:
        count = len(points)
        picked = []

        if max_points == 1:
            indices = [count // 2]

        else:
            indices = [
                int(round(index * (count - 1) / (max_points - 1)))
                for index in range(max_points)
            ]

        for index in indices:
            candidate = points[index]

            if candidate not in picked:
                picked.append(candidate)

        points = picked

    return points


def render_points(points: list[Point]) -> str:
    lines = [
        f"{index + 1}. [{point.start:.1f}–{point.end:.1f}] {point.text}"
        for index, point in enumerate(points)
    ]

    return "\n".join(lines)


def parse_citations(text: str) -> tuple[int, ...]:
    """从文本里解析 `[12]` / `[12-14]` 形式的段号（评测用）。"""

    found: list[int] = []

    for match in _CITATION.finditer(text or ""):
        start = int(match.group(1))

        if start not in found:
            found.append(start)

    return tuple(found)
