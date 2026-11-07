"""产物 Diff（v1.3.1）：文本 unified diff / 二进制摘要。"""

from src.diff.compare import (
    DEFAULT_CONTEXT,
    MAX_BYTES,
    DiffError,
    DiffLine,
    DiffResult,
    compare_artifacts,
    compare_files,
    compare_text,
    decode_text,
    read_bytes,
)


__all__ = [
    "DEFAULT_CONTEXT",
    "MAX_BYTES",
    "DiffError",
    "DiffLine",
    "DiffResult",
    "compare_artifacts",
    "compare_files",
    "compare_text",
    "decode_text",
    "read_bytes",
]
