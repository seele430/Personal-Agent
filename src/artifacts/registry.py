"""产物登记表 + 路径安全。

**路径安全是这一层的核心**，不是附属功能：

- 产物根目录固定为 `<data_dir>/artifacts/`（`backup.py` 已把它列进备份清单）；
- 登记与读取**都**重新做一次"落在根目录之内"的判定（`Path.resolve()` +
  `relative_to`），所以 `../`、绝对路径、符号链接逃逸、同级前缀目录
  （`artifacts-evil/`）全部拦在门外；
- 判定用 `relative_to` 而不是字符串前缀比较 —— `startswith` 会把
  `/root/artifacts-evil/x` 当成根目录里的文件。
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid

from contextlib import closing
from pathlib import Path

from src.artifacts.models import (
    MAX_ARTIFACT_BYTES,
    Artifact,
    ArtifactError,
    ArtifactForbidden,
    ArtifactNotFound,
    classify,
    guess_media_type,
    utc_now,
    validate_owner,
    validate_title,
)


DEFAULT_ROOT_NAME = "artifacts"
CHUNK_SIZE = 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    title       TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    media_type  TEXT NOT NULL,
    kind        TEXT NOT NULL
                CHECK (kind IN ('image','text','markdown','pdf','other')),
    size_bytes  INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    run_id      TEXT NOT NULL DEFAULT '',
    exec_id     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_owner
    ON artifacts (owner_id, created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_run
    ON artifacts (run_id);
"""


class ArtifactRegistry:
    def __init__(self, db_path: str | Path | None = None, *, root: str | Path | None = None):
        from src import config

        data_dir = config.paths.chat_requests_db.parent

        self.db_path = Path(db_path or (data_dir / "artifacts.db"))
        self.root = Path(root or (data_dir / DEFAULT_ROOT_NAME))

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)

        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def init_db(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(_SCHEMA)

    # ---- 路径安全 ---------------------------------------------------

    def _ensure_inside(self, candidate: Path) -> Path:
        """把候选路径解析成 canonical path，并在越界时拒绝。"""

        resolved = candidate.resolve()
        root = self.root.resolve()

        try:
            resolved.relative_to(root)

        except ValueError:
            raise ArtifactForbidden("产物路径越界") from None

        return resolved

    def _to_relative(self, resolved: Path) -> str:
        return resolved.relative_to(self.root.resolve()).as_posix()

    # ---- 登记 -------------------------------------------------------

    def register(
        self,
        owner_id: str,
        path: str | Path,
        *,
        title: str | None = None,
        media_type: str = "",
        run_id: str = "",
        exec_id: str = "",
    ) -> Artifact:
        owner_id = validate_owner(owner_id)

        candidate = Path(path)

        if not candidate.is_absolute():
            candidate = self.root / candidate

        resolved = self._ensure_inside(candidate)

        if not resolved.exists():
            raise ArtifactNotFound(f"产物文件不存在：{resolved.name}")

        if not resolved.is_file():
            raise ArtifactError("只能登记文件，不能登记目录")

        size = resolved.stat().st_size

        if size > MAX_ARTIFACT_BYTES:
            raise ArtifactError(
                f"产物过大：{size} 字节 > 上限 {MAX_ARTIFACT_BYTES}"
            )

        digest = self._sha256(resolved)
        resolved_type = (media_type or guess_media_type(resolved.name)).strip()
        kind = classify(resolved_type, resolved.name)

        artifact_id = f"art_{uuid.uuid4().hex}"
        rel_path = self._to_relative(resolved)

        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO artifacts ("
                " id, owner_id, title, rel_path, media_type, kind,"
                " size_bytes, sha256, run_id, exec_id, created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    owner_id,
                    validate_title(title, fallback=resolved.name),
                    rel_path,
                    resolved_type[:100],
                    kind,
                    size,
                    digest,
                    (run_id or "")[:100],
                    (exec_id or "")[:100],
                    utc_now(),
                ),
            )

        return self.get(artifact_id, owner_id=owner_id)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()

        with path.open("rb") as handle:
            while True:
                chunk = handle.read(CHUNK_SIZE)

                if not chunk:
                    break

                digest.update(chunk)

        return digest.hexdigest()

    # ---- 读取 -------------------------------------------------------

    def get(self, artifact_id: str, *, owner_id: str) -> Artifact:
        owner_id = validate_owner(owner_id)

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()

        if row is None:
            raise ArtifactNotFound(f"产物不存在：{artifact_id}")

        if row["owner_id"] != owner_id:
            # 不区分"不存在"和"无权"会给越权探测留口子，这里统一按越权处理。
            raise ArtifactForbidden("无权访问该产物")

        return _to_artifact(row)

    def list(
        self,
        owner_id: str,
        *,
        run_id: str | None = None,
        limit: int = 200,
    ) -> list[Artifact]:
        owner_id = validate_owner(owner_id)

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")

        clauses = ["owner_id = ?"]
        params: list = [owner_id]

        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)

        params.append(limit)

        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()

        return [_to_artifact(row) for row in rows]

    def absolute_path(self, artifact_id: str, *, owner_id: str) -> Path:
        """取回文件真实路径 —— **读的时候再校验一次**落在根目录内。"""

        artifact = self.get(artifact_id, owner_id=owner_id)
        resolved = self._ensure_inside(self.root / artifact.rel_path)

        if not resolved.is_file():
            raise ArtifactNotFound("产物文件已不存在")

        return resolved

    def count(self, owner_id: str | None = None) -> int:
        query = "SELECT COUNT(*) AS n FROM artifacts"
        params: list = []

        if owner_id is not None:
            query += " WHERE owner_id = ?"
            params.append(validate_owner(owner_id))

        with closing(self._connect()) as connection:
            row = connection.execute(query, params).fetchone()

        return int(row["n"])

    # ---- 删除 -------------------------------------------------------

    def delete(self, artifact_id: str, *, owner_id: str, purge_file: bool = False) -> bool:
        artifact = self.get(artifact_id, owner_id=owner_id)

        if purge_file:
            try:
                path = self._ensure_inside(self.root / artifact.rel_path)
                path.unlink(missing_ok=True)

            except (ArtifactForbidden, OSError):
                # 文件删不掉不该让账本删不掉：记录仍删除，文件留给人工处理。
                pass

        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))

        return True


def _to_artifact(row) -> Artifact:
    return Artifact(
        id=row["id"],
        owner_id=row["owner_id"],
        title=row["title"],
        rel_path=row["rel_path"],
        media_type=row["media_type"],
        kind=row["kind"],
        size_bytes=int(row["size_bytes"]),
        sha256=row["sha256"],
        run_id=row["run_id"],
        exec_id=row["exec_id"],
        created_at=row["created_at"],
    )


_REGISTRY = None
_REGISTRY_KEY = None


def get_registry() -> ArtifactRegistry:
    global _REGISTRY, _REGISTRY_KEY

    from src import config

    data_dir = config.paths.chat_requests_db.parent

    if _REGISTRY is None or _REGISTRY_KEY != data_dir:
        _REGISTRY = ArtifactRegistry()
        _REGISTRY_KEY = data_dir

    return _REGISTRY


def reset_registry() -> None:
    global _REGISTRY, _REGISTRY_KEY
    _REGISTRY = None
    _REGISTRY_KEY = None
