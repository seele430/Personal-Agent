"""产物原子落盘：路径边界 + sha256 + 字节数。

规则：

- 只接受产物根目录内的**相对路径**；
- 拒绝绝对路径、`..`、盘符、UNC、符号链接/重解析点逃逸；
- staging 与正式目录同盘，校验后 `os.replace` 原子提交；
- 摘要或字节数不一致视为失败，不进入 succeeded。
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile

from pathlib import Path


class ArtifactError(ValueError):
    """产物不合法。"""


DRIVE = re.compile(r"^[A-Za-z]:")


def _check_rel(rel_path: str) -> Path:
    text = (rel_path or "").strip()

    if not text:
        raise ArtifactError("产物路径不能为空")

    if DRIVE.match(text) or text.startswith("\\\\") or text.startswith("//"):
        raise ArtifactError("只允许产物根目录内的相对路径")

    parts = Path(text).parts

    if any(part in ("..", "") for part in parts):
        raise ArtifactError("产物路径不允许 ..")

    return Path(*parts)


def commit(root: str | Path, rel_path: str, data: bytes) -> dict:
    root_path = Path(root).resolve()
    root_path.mkdir(parents=True, exist_ok=True)
    rel = _check_rel(rel_path)

    current = root_path

    for part in rel.parts[:-1]:
        current = current / part

        if not current.exists():
            continue

        attributes = getattr(current.stat(), "st_file_attributes", 0)

        if current.is_symlink() or attributes & 0x400:
            raise ArtifactError("产物路径不允许符号链接")

    target = (root_path / rel).resolve()

    if not target.is_relative_to(root_path):
        raise ArtifactError("产物越界")

    target.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = root_path / ".staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(dir=staging_dir, delete=False) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = handle.name

    digest = hashlib.sha256(data).hexdigest()
    os.replace(temporary, target)

    return {
        "path": str(rel).replace("\\", "/"),
        "sha256": digest,
        "bytes": len(data),
    }


def verify(root: str | Path, manifest: dict) -> bool:
    try:
        path = (Path(root).resolve() / manifest["path"]).resolve()
        data = path.read_bytes()
    except (OSError, KeyError):
        return False

    return (
        len(data) == int(manifest.get("bytes", -1))
        and hashlib.sha256(data).hexdigest() == manifest.get("sha256")
    )
