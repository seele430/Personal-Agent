"""四类预览载荷：图片 / 文本 / Markdown / PDF，其余只能下载。

预览**不复制文件内容到响应里**（图片与 PDF 走 `content` 端点，由浏览器直接取），
文本类才内联返回，并且有明确上限：最多读 256 KB、最多回 20000 字符。
这样"预览一个 50 MB 的日志"不会把内存和服务一起拖垮。
"""

from __future__ import annotations

from pathlib import Path

from src.artifacts.models import Artifact


MAX_TEXT_CHARS = 20_000
MAX_READ_BYTES = 256 * 1024

CONTENT_URL = "/api/artifacts/{artifact_id}/content"


def preview_payload(
    artifact: Artifact,
    path: Path,
    *,
    max_text_chars: int = MAX_TEXT_CHARS,
    max_read_bytes: int = MAX_READ_BYTES,
) -> dict:
    """按产物类型生成预览载荷。"""

    if isinstance(max_text_chars, bool) or not isinstance(max_text_chars, int):
        raise ValueError("max_text_chars 必须是整数")

    if not 1 <= max_text_chars <= MAX_TEXT_CHARS:
        raise ValueError(f"max_text_chars 必须在 1..{MAX_TEXT_CHARS}")

    base = {
        "id": artifact.id,
        "title": artifact.title,
        "kind": artifact.kind,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
    }

    if artifact.kind in ("image", "pdf"):
        return {
            **base,
            "previewable": True,
            "mode": "inline",
            "url": CONTENT_URL.format(artifact_id=artifact.id),
        }

    if artifact.kind in ("text", "markdown"):
        text, truncated = _read_text(path, max_read_bytes, max_text_chars)

        return {
            **base,
            "previewable": True,
            "mode": artifact.kind,
            "text": text,
            "truncated": truncated,
        }

    return {
        **base,
        "previewable": False,
        "mode": "download",
        "note": "该类型不支持预览，请下载后查看",
        "url": CONTENT_URL.format(artifact_id=artifact.id),
    }


def _read_text(path: Path, max_bytes: int, max_chars: int) -> tuple[str, bool]:
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)

    truncated = len(raw) > max_bytes or path.stat().st_size > max_bytes
    text = raw[:max_bytes].decode("utf-8", "replace")

    if len(text) > max_chars:
        return text[:max_chars], True

    return text, truncated
