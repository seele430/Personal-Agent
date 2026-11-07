"""产物（artifact）的领域模型：类型判定与字段校验。

产物是"研究/执行跑出来的文件"，不是数据库里的一条聊天记录。所以它的
安全边界比普通资源更硬：**路径必须落在产物根目录之内**，且类型判定
不能只信文件名（扩展名可以随便改，media_type 也可以伪造）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


#: 四类预览 + 兜底下载
KINDS = ("image", "text", "markdown", "pdf", "other")

MAX_TITLE_CHARS = 200
MAX_OWNER_CHARS = 100
MAX_MEDIA_TYPE_CHARS = 100

#: 单文件上限：超过就不登记（避免把整盘数据塞进产物库）
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})
_TEXT_SUFFIXES = frozenset({".txt", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml", ".py", ".ts", ".js", ".html", ".css"})
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
_PDF_SUFFIXES = frozenset({".pdf"})

_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/svg+xml"})
_TEXT_TYPES = frozenset({"text/plain", "text/csv", "application/json", "text/yaml", "application/x-yaml"})
_MARKDOWN_TYPES = frozenset({"text/markdown", "text/x-markdown"})
_PDF_TYPES = frozenset({"application/pdf"})


class ArtifactError(ValueError):
    """产物不合法（路径、大小、类型）。"""


class ArtifactForbidden(PermissionError):
    """越界或越权。"""


class ArtifactNotFound(KeyError):
    """产物不存在。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify(media_type: str, filename: str) -> str:
    """判定预览类型：先看 media_type，再看扩展名，都不认识就 `other`。"""

    normalized = (media_type or "").split(";", 1)[0].strip().lower()

    if normalized in _IMAGE_TYPES:
        return "image"

    if normalized in _PDF_TYPES:
        return "pdf"

    if normalized in _MARKDOWN_TYPES:
        return "markdown"

    if normalized in _TEXT_TYPES:
        # text/plain 既可能是 .md 也可能只是日志：扩展名优先细化
        suffix = Path(filename).suffix.lower()

        if suffix in _MARKDOWN_SUFFIXES:
            return "markdown"

        return "text"

    suffix = Path(filename).suffix.lower()

    if suffix in _IMAGE_SUFFIXES:
        return "image"

    if suffix in _PDF_SUFFIXES:
        return "pdf"

    if suffix in _MARKDOWN_SUFFIXES:
        return "markdown"

    if suffix in _TEXT_SUFFIXES:
        return "text"

    return "other"


def guess_media_type(filename: str) -> str:
    """按扩展名给一个保守的 media_type（不依赖外部库）。"""

    suffix = Path(filename).suffix.lower()

    table = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".svg": "image/svg+xml",
        ".pdf": "application/pdf",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".json": "application/json",
        ".csv": "text/csv",
        ".yaml": "text/yaml",
        ".yml": "text/yaml",
        ".txt": "text/plain",
        ".log": "text/plain",
    }

    return table.get(suffix, "application/octet-stream")


def validate_owner(owner_id) -> str:
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ArtifactError("owner_id 不能为空")

    if len(owner_id.strip()) > MAX_OWNER_CHARS:
        raise ArtifactError("owner_id 过长")

    return owner_id.strip()


def validate_title(title, *, fallback: str) -> str:
    if title is None:
        text = fallback

    elif not isinstance(title, str):
        raise ArtifactError("title 必须是字符串")

    else:
        text = " ".join(title.split()) or fallback

    if len(text) > MAX_TITLE_CHARS:
        raise ArtifactError(f"title 超过 {MAX_TITLE_CHARS} 字符")

    return text


@dataclass(frozen=True)
class Artifact:
    id: str
    owner_id: str
    title: str
    rel_path: str
    media_type: str
    kind: str
    size_bytes: int
    sha256: str
    run_id: str
    exec_id: str
    created_at: str

    @property
    def is_previewable(self) -> bool:
        return self.kind in ("image", "text", "markdown", "pdf")
