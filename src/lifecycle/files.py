"""文件生命周期：按修改时间清理报告 / 日志 / 关键帧。

三条护栏（与数据库清理同风格）：

1. **默认预演**：`plan_files()` 只列候选；
2. **删除必须确认**：`purge_files(plan, confirm=True)`；
3. **边界**：只删指定目录内、指定后缀的**普通文件**；符号链接一律跳过，
   目录本身不删。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_SUFFIXES = (".md", ".jsonl", ".jpg", ".png")


class FileLifecycleError(ValueError):
    """参数不合法。"""


@dataclass(frozen=True)
class FileCandidate:
    path: Path
    size_bytes: int
    mtime: str

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "mtime": self.mtime,
        }


@dataclass(frozen=True)
class FilePlan:
    root: Path
    cutoff: str
    candidates: tuple[FileCandidate, ...] = ()
    skipped_links: int = 0

    @property
    def count(self) -> int:
        return len(self.candidates)

    @property
    def bytes(self) -> int:
        return sum(item.size_bytes for item in self.candidates)

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "cutoff": self.cutoff,
            "count": self.count,
            "bytes": self.bytes,
            "skipped_links": self.skipped_links,
            "candidates": [item.to_dict() for item in self.candidates],
        }


def plan_files(
    root: str | Path,
    *,
    days: int = 30,
    suffixes: tuple[str, ...] = DEFAULT_SUFFIXES,
    now: datetime | None = None,
) -> FilePlan:
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise FileLifecycleError("days 必须是 ≥1 的整数")

    normalized = tuple(
        (suffix if suffix.startswith(".") else f".{suffix}").lower()
        for suffix in suffixes
    )

    if not normalized:
        raise FileLifecycleError("suffixes 不能为空")

    base = Path(root)

    if not base.is_dir():
        return FilePlan(
            root=base,
            cutoff=(now or datetime.now(timezone.utc)).isoformat(),
        )

    moment = now or datetime.now(timezone.utc)
    cutoff = (moment - timedelta(days=days)).timestamp()

    candidates = []
    skipped = 0

    for item in base.iterdir():
        if item.is_symlink():
            skipped += 1
            continue

        if not item.is_file() or item.suffix.lower() not in normalized:
            continue

        try:
            stat = item.stat()

        except OSError:
            continue

        if stat.st_mtime < cutoff:
            candidates.append(
                FileCandidate(
                    path=item,
                    size_bytes=stat.st_size,
                    mtime=datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                )
            )

    candidates.sort(key=lambda entry: entry.mtime)

    return FilePlan(
        root=base,
        cutoff=datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(),
        candidates=tuple(candidates),
        skipped_links=skipped,
    )


def purge_files(plan_value: FilePlan, *, confirm: bool = False) -> dict:
    if not confirm:
        raise FileLifecycleError("删除前必须显式确认：purge_files(..., confirm=True)")

    deleted = 0
    freed = 0

    for candidate in plan_value.candidates:
        try:
            candidate.path.unlink()
            deleted += 1
            freed += candidate.size_bytes

        except OSError:
            # 删不掉的文件不掩盖其它删除；计数如实反映
            continue

    return {"deleted": deleted, "freed_bytes": freed, "planned": plan_value.count}
