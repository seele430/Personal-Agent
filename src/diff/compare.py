"""产物差异：文本走 unified diff，二进制只给"变了没有 + 大小 + 哈希"。

三条底线：

1. **不猜乱码**：UTF-8 解不了退 GBK，再不行就当二进制；
2. **有上限**：默认只比较前 2 MB，超出标 truncated；
3. **只读**：不修改任何文件（调用方可用哈希自证）。
"""

from __future__ import annotations

import difflib
import hashlib

from dataclasses import dataclass, field
from pathlib import Path


MAX_BYTES = 2 * 1024 * 1024
DEFAULT_CONTEXT = 3


class DiffError(ValueError):
    """参数或输入不合法。"""


@dataclass(frozen=True)
class DiffLine:
    kind: str          # add / remove / context
    text: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text}


@dataclass(frozen=True)
class DiffResult:
    kind: str                       # text / binary / identical
    a_name: str
    b_name: str
    added: int = 0
    removed: int = 0
    lines: tuple[DiffLine, ...] = ()
    truncated: bool = False
    a_size: int = 0
    b_size: int = 0
    a_digest: str = ""
    b_digest: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed) or self.kind == "binary"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "a_name": self.a_name,
            "b_name": self.b_name,
            "added": self.added,
            "removed": self.removed,
            "truncated": self.truncated,
            "changed": self.changed,
            "a_size": self.a_size,
            "b_size": self.b_size,
            "a_digest": self.a_digest,
            "b_digest": self.b_digest,
            "lines": [line.to_dict() for line in self.lines],
        }


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def read_bytes(path: str | Path, *, max_bytes: int = MAX_BYTES) -> tuple[bytes, bool]:
    """读前 max_bytes，返回 (数据, 是否截断)。"""

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise DiffError("max_bytes 必须是正整数")

    target = Path(path)

    if not target.is_file():
        raise DiffError(f"文件不存在：{target}")

    size = target.stat().st_size

    with target.open("rb") as handle:
        data = handle.read(max_bytes)

    return data, size > max_bytes


def decode_text(data: bytes) -> str | None:
    """UTF-8 → GBK → 放弃（返回 None 表示不是文本）。"""

    if b"\x00" in data[:4096]:
        return None

    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)

        except UnicodeDecodeError:
            continue

    return None


def compare_text(
    a_name: str,
    b_name: str,
    a_text: str,
    b_text: str,
    *,
    context: int = DEFAULT_CONTEXT,
) -> DiffResult:
    if context < 0:
        raise DiffError("context 不能为负")

    a_lines = a_text.splitlines()
    b_lines = b_text.splitlines()

    lines: list[DiffLine] = []
    added = removed = 0

    for raw in difflib.unified_diff(
        a_lines, b_lines, fromfile=a_name, tofile=b_name, lineterm="", n=context
    ):
        if raw.startswith("@@"):
            continue

        if raw.startswith("+") and not raw.startswith("+++"):
            kind, text = "add", raw[1:]
            added += 1

        elif raw.startswith("-") and not raw.startswith("---"):
            kind, text = "remove", raw[1:]
            removed += 1

        elif raw.startswith(("+", "-")):
            continue

        else:
            kind, text = "context", raw[1:] if raw.startswith(" ") else raw

        lines.append(DiffLine(kind=kind, text=text))

    kind = "text" if (added or removed) else "identical"

    return DiffResult(
        kind=kind,
        a_name=a_name,
        b_name=b_name,
        added=added,
        removed=removed,
        lines=tuple(lines),
    )


def compare_files(
    a: str | Path,
    b: str | Path,
    *,
    max_bytes: int = MAX_BYTES,
    context: int = DEFAULT_CONTEXT,
) -> DiffResult:
    a_path, b_path = Path(a), Path(b)

    a_data, a_truncated = read_bytes(a_path, max_bytes=max_bytes)
    b_data, b_truncated = read_bytes(b_path, max_bytes=max_bytes)

    a_text = decode_text(a_data)
    b_text = decode_text(b_data)

    if a_text is None or b_text is None:
        return DiffResult(
            kind="identical" if a_data == b_data else "binary",
            a_name=a_path.name,
            b_name=b_path.name,
            truncated=a_truncated or b_truncated,
            a_size=a_path.stat().st_size,
            b_size=b_path.stat().st_size,
            a_digest=_digest(a_data),
            b_digest=_digest(b_data),
        )

    result = compare_text(
        a_path.name, b_path.name, a_text, b_text, context=context
    )

    return DiffResult(
        kind=result.kind,
        a_name=result.a_name,
        b_name=result.b_name,
        added=result.added,
        removed=result.removed,
        lines=result.lines,
        truncated=a_truncated or b_truncated,
        a_size=a_path.stat().st_size,
        b_size=b_path.stat().st_size,
        a_digest=_digest(a_data),
        b_digest=_digest(b_data),
    )


def compare_artifacts(registry, a_id: str, b_id: str, *, owner_id: str, **kwargs) -> DiffResult:
    """比较两个产物；越权由 registry.absolute_path 负责抛错。"""

    a_path = registry.absolute_path(a_id, owner_id=owner_id)
    b_path = registry.absolute_path(b_id, owner_id=owner_id)

    return compare_files(a_path, b_path, **kwargs)
