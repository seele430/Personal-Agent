"""Qwen Runner 的持久运行目录与产物扫描。

目录：

```text
data/qwen-runs/<run_id>/
├── state.json       # Runner 状态（原子写）
├── manifest.json    # 产物清单
└── workspace/       # Qwen 的 cwd；唯一产物扫描根
```

安全边界：`workspace` 只限制初始目录与回收范围，**不是 OS 沙箱**。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat

from pathlib import Path


MAX_FILES = 10
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024

REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class WorkspaceError(ValueError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class ArtifactError(ValueError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class RunWorkspace:
    def __init__(self, root: str | Path, run_id: str):
        self.root = Path(root) / run_id
        self.workspace = self.root / "workspace"
        self.state_path = self.root / "state.json"
        self.manifest_path = self.root / "manifest.json"

    def create(self) -> "RunWorkspace":
        if self.root.exists():
            raise WorkspaceError("run_exists", f"运行目录已存在：{self.root}")

        self.workspace.mkdir(parents=True)

        return self

    def write_state(self, state: dict) -> None:
        _atomic_json(self.state_path, state)

    def read_state(self) -> dict | None:
        if not self.state_path.exists():
            return None

        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def scan(self, expected: dict[str, bool] | None = None) -> list[dict]:
        """扫描 workspace 中的常规文件；返回 manifest 列表。"""

        expected = expected or {}
        files: list[Path] = []

        for path in self.workspace.rglob("*"):
            if path.is_symlink():
                raise ArtifactError("artifact_rejected", f"不允许符号链接 {path.name}")

            if path.is_file():
                files.append(path)

        if len(files) > MAX_FILES:
            raise ArtifactError("artifact_rejected", f"文件数 {len(files)} 超过上限 {MAX_FILES}")

        manifest: list[dict] = []
        total = 0
        seen: set[str] = set()

        for path in files:
            relative = path.relative_to(self.workspace).as_posix()
            _check_relative(path, self.workspace)
            size = path.stat().st_size
            total += size

            if size > MAX_FILE_BYTES:
                raise ArtifactError("artifact_rejected", f"{relative} 超过单文件上限")

            if total > MAX_TOTAL_BYTES:
                raise ArtifactError("artifact_rejected", "产物总大小超过上限")

            data = path.read_bytes()
            manifest.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
            )
            seen.add(relative)

        for relative, allow_empty in expected.items():
            if relative not in seen:
                raise ArtifactError("artifact_missing", f"缺少预期产物 {relative}")

            if not allow_empty and (self.workspace / relative).stat().st_size == 0:
                raise ArtifactError("artifact_missing", f"预期产物为空 {relative}")

        _atomic_json(
            self.manifest_path,
            {"run_id": self.root.name, "scanned_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(), "scan_result": "ok",
             "artifacts": manifest, "limits": {
                "max_files": MAX_FILES,
                "max_file_bytes": MAX_FILE_BYTES,
                "max_total_bytes": MAX_TOTAL_BYTES,
            }},
        )

        return manifest


def _check_relative(path: Path, root: Path) -> None:
    relative = path.relative_to(root)

    if not relative.parts or any(part in ("..", "") for part in relative.parts):
        raise ArtifactError("artifact_rejected", "非法相对路径")

    current = root

    for part in relative.parts:
        current = current / part

        if current.exists():
            attributes = getattr(current.stat(), "st_file_attributes", 0)

            if current.is_symlink() or attributes & REPARSE_FLAG:
                raise ArtifactError("artifact_rejected", "不允许符号链接或重解析点")

    if not path.resolve().is_relative_to(root.resolve()):
        raise ArtifactError("artifact_rejected", "产物越界")


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2))
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(temporary, path)
