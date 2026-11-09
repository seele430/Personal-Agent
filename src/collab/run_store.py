"""接力任务存储：run / revision / step / attempt（SQLite）。

关键不变量：

- 认领只允许 `pending`（或租约过期的 `running`）；影响行数必须为 1；
- 每次认领 `fencing_token + 1`；所有写入都校验 token，旧 worker 一律 `stale_worker`；
- `needs_review` 不允许被普通 `resume` 自动执行；
- `continue` 只允许 `succeeded`，revision + 1，历史步骤保留。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid

from contextlib import contextmanager
from pathlib import Path


DEFAULT_LEASE_SECONDS = 120.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS collab_runs (
    run_id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    chain_json TEXT NOT NULL,
    next_provider_index INTEGER NOT NULL DEFAULT 0,
    provider TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at REAL,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    messages_json TEXT,
    error_class TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS collab_steps (
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    step_id TEXT NOT NULL,
    state TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    artifact_manifest TEXT,
    fencing_token INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (run_id, revision, step_id),
    UNIQUE (run_id, revision, idempotency_key)
);
CREATE TABLE IF NOT EXISTS collab_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    event TEXT NOT NULL,
    detail TEXT,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS collab_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    provider TEXT NOT NULL,
    state TEXT NOT NULL,
    error_class TEXT,
    error TEXT,
    injected INTEGER NOT NULL DEFAULT 0,
    fencing_token INTEGER NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    UNIQUE (run_id, revision, attempt_no)
);
"""


class StaleWorker(RuntimeError):
    """旧 worker 的写入被 fencing 拒绝。"""


class StoreError(ValueError):
    """状态机拒绝。"""


def idempotency_key(run_id: str, revision: int, step_id: str, task: str) -> str:
    payload = f"{run_id}|{revision}|{step_id}|{task}".encode("utf-8")

    return hashlib.sha256(payload).hexdigest()


class RunStore:
    def __init__(self, path: str | Path = "data/collab-runs.db", *, clock=None):
        self.path = str(path)
        self._clock = clock or time.time
        self._init()

    # ---------- 连接 ----------

    @contextmanager
    def _session(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")

        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        with self._session() as connection:
            connection.executescript(SCHEMA)

    # ---------- 创建/查询 ----------

    def create_run(self, task: str, chain: list[str], *, messages: list[dict] | None = None) -> str:
        run_id = uuid.uuid4().hex
        now = self._clock()

        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO collab_runs
                    (run_id, task, state, revision, chain_json, next_provider_index,
                     attempts, messages_json, created_at, updated_at)
                VALUES (?, ?, 'pending', 1, ?, 0, 0, ?, ?, ?)
                """,
                (
                    run_id,
                    task,
                    json.dumps(chain),
                    json.dumps(messages or [], ensure_ascii=False),
                    now,
                    now,
                ),
            )

        return run_id

    def get(self, run_id: str) -> dict | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM collab_runs WHERE run_id = ?", (run_id,)
            ).fetchone()

            return self._decode_run(row) if row else None

    @staticmethod
    def _decode_run(row) -> dict:
        data = dict(row)
        data["chain"] = json.loads(data.pop("chain_json"))

        return data

    # ---------- 认领 / 心跳 / 提交 ----------

    def claim(self, run_id: str, owner: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS):
        now = self._clock()
        expires = now + lease_seconds

        with self._session() as connection:
            cursor = connection.execute(
                "UPDATE collab_runs SET state='running', lease_owner=?, lease_expires_at=?, "
                "fencing_token=fencing_token + 1, updated_at=? "
                "WHERE run_id=? AND (state='pending' "
                "OR (state='running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?))",
                (owner, expires, now, run_id, now),
            )

            if cursor.rowcount != 1:
                return None

            row = connection.execute(
                "SELECT * FROM collab_runs WHERE run_id = ?", (run_id,)
            ).fetchone()

            return self._decode_run(row)

    def heartbeat(self, run_id: str, token: int, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> bool:
        now = self._clock()

        with self._session() as connection:
            cursor = connection.execute(
                "UPDATE collab_runs SET lease_expires_at=?, updated_at=? "
                "WHERE run_id=? AND fencing_token=? AND state='running'",
                (now + lease_seconds, now, run_id, int(token)),
            )

            return cursor.rowcount == 1

    def reset_chain(self, run_id: str) -> bool:
        with self._session() as connection:
            cursor = connection.execute(
                "UPDATE collab_runs SET next_provider_index=0, updated_at=? "
                "WHERE run_id=? AND state='failed'",
                (self._clock(), run_id),
            )

            return cursor.rowcount == 1

    def set_chain(self, run_id: str, chain: list[str]) -> bool:
        with self._session() as connection:
            cursor = connection.execute(
                "UPDATE collab_runs SET chain_json=?, next_provider_index=0, updated_at=? "
                "WHERE run_id=? AND state IN ('pending','failed','succeeded')",
                (json.dumps(chain), self._clock(), run_id),
            )

            return cursor.rowcount == 1

    def set_next_provider(self, run_id: str, token: int, index: int, provider: str | None) -> bool:
        with self._session() as connection:
            cursor = connection.execute(
                "UPDATE collab_runs SET next_provider_index=?, provider=?, updated_at=? "
                "WHERE run_id=? AND fencing_token=?",
                (int(index), provider, self._clock(), run_id, int(token)),
            )

            return cursor.rowcount == 1

    def finish(self, run_id: str, token: int, state: str, *, provider=None, error_class=None, error=None) -> bool:
        with self._session() as connection:
            cursor = connection.execute(
                "UPDATE collab_runs SET state=?, provider=COALESCE(?, provider), "
                "error_class=?, error=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE run_id=? AND fencing_token=?",
                (state, provider, error_class, error, self._clock(), run_id, int(token)),
            )

            return cursor.rowcount == 1

    def mark_needs_review(self, run_id: str, token: int, reason: str) -> bool:
        with self._session() as connection:
            cursor = connection.execute(
                "UPDATE collab_runs SET state='needs_review', error=?, updated_at=? "
                "WHERE run_id=? AND fencing_token=?",
                (reason, self._clock(), run_id, int(token)),
            )

            return cursor.rowcount == 1

    # ---------- resume / continue ----------

    def resume(self, run_id: str) -> dict:
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM collab_runs WHERE run_id = ?", (run_id,)
            ).fetchone()

            if row is None:
                raise StoreError("run 不存在")

            state = row["state"]

            if state == "needs_review":
                raise StoreError("needs_review：请用 --accept-step 或 --retry-step 明确处理")

            if state == "succeeded":
                raise StoreError("already_succeeded")

            if state == "running":
                expires = row["lease_expires_at"]

                if expires is None or float(expires) >= self._clock():
                    raise StoreError("running：租约未过期，不能 resume")

                connection.execute(
                    "UPDATE collab_runs SET state='pending', error=NULL, error_class=NULL, updated_at=? "
                    "WHERE run_id=? AND state='running'",
                    (self._clock(), run_id),
                )

                row = connection.execute(
                    "SELECT * FROM collab_runs WHERE run_id = ?", (run_id,)
                ).fetchone()

                return self._decode_run(row)

            connection.execute(
                "UPDATE collab_runs SET state='pending', error=NULL, "
                "error_class=NULL, updated_at=? WHERE run_id=? AND state='failed'",
                (self._clock(), run_id),
            )

            row = connection.execute(
                "SELECT * FROM collab_runs WHERE run_id = ?", (run_id,)
            ).fetchone()

            return self._decode_run(row)

    def continue_run(self, run_id: str, message: str) -> dict:
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM collab_runs WHERE run_id = ?", (run_id,)
            ).fetchone()

            if row is None:
                raise StoreError("run 不存在")

            if row["state"] != "succeeded":
                raise StoreError("只有 succeeded 的 run 可以 continue")

            messages = json.loads(row["messages_json"] or "[]")
            messages.append({"role": "user", "content": message})

            connection.execute(
                "UPDATE collab_runs SET revision=revision + 1, state='pending', "
                "next_provider_index=0, error=NULL, error_class=NULL, messages_json=?, updated_at=? "
                "WHERE run_id=? AND state='succeeded'",
                (json.dumps(messages, ensure_ascii=False), self._clock(), run_id),
            )

            row = connection.execute(
                "SELECT * FROM collab_runs WHERE run_id = ?", (run_id,)
            ).fetchone()

            return self._decode_run(row)

    # ---------- 步骤 ----------

    def step_begin(self, run_id: str, revision: int, step_id: str, input_digest: str, token: int) -> str:
        """返回 execute / skip / needs_review；旧 token 抛 StaleWorker。"""

        with self._session() as connection:
            run_row = connection.execute(
                "SELECT fencing_token, revision, state FROM collab_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()

            if (
                run_row is None
                or int(run_row["revision"]) != int(revision)
                or int(run_row["fencing_token"]) != int(token)
                or run_row["state"] != "running"
            ):
                # 旧 token：不做任何写入，返回哨兵，由调用方放弃
                return "stale"

            row = connection.execute(
                "SELECT * FROM collab_steps WHERE run_id=? AND revision=? AND step_id=?",
                (run_id, revision, step_id),
            ).fetchone()

            if row and row["state"] == "done":
                return "skip"

            if row and row["state"] in ("running", "needs_review"):
                connection.execute(
                    "UPDATE collab_steps SET state='needs_review', fencing_token=?, updated_at=? "
                    "WHERE run_id=? AND revision=? AND step_id=?",
                    (int(token), self._clock(), run_id, revision, step_id),
                )

                return "needs_review"

            key = idempotency_key(run_id, revision, step_id, input_digest)

            if row is None:
                connection.execute(
                    "INSERT INTO collab_steps (run_id, revision, step_id, state, idempotency_key, "
                    "fencing_token, attempts, updated_at) VALUES (?, ?, ?, 'running', ?, ?, 1, ?)",
                    (run_id, revision, step_id, key, int(token), self._clock()),
                )
            else:
                connection.execute(
                    "UPDATE collab_steps SET state='running', fencing_token=?, attempts=attempts + 1, "
                    "updated_at=? WHERE run_id=? AND revision=? AND step_id=?",
                    (int(token), self._clock(), run_id, revision, step_id),
                )

            return "execute"

    def step_done(self, run_id: str, revision: int, step_id: str, token: int, manifest: dict | None) -> bool:
        with self._session() as connection:
            if not self._run_owned(connection, run_id, revision, token, ("running",)):
                return False

            cursor = connection.execute(
                "UPDATE collab_steps SET state='done', artifact_manifest=?, updated_at=? "
                "WHERE run_id=? AND revision=? AND step_id=? AND fencing_token=?",
                (
                    json.dumps(manifest, ensure_ascii=False) if manifest else None,
                    self._clock(),
                    run_id,
                    revision,
                    step_id,
                    int(token),
                ),
            )

            return cursor.rowcount == 1

    def step_manifest(self, run_id: str, revision: int, step_id: str) -> dict | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT artifact_manifest FROM collab_steps WHERE run_id=? AND revision=? AND step_id=?",
                (run_id, revision, step_id),
            ).fetchone()

            if row is None or not row["artifact_manifest"]:
                return None

            return json.loads(row["artifact_manifest"])

    def audit(self, run_id: str, revision: int, event: str, detail: dict | None = None) -> None:
        with self._session() as connection:
            connection.execute(
                "INSERT INTO collab_events (run_id, revision, event, detail, at) VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    revision,
                    event,
                    json.dumps(detail or {}, ensure_ascii=False),
                    self._clock(),
                ),
            )

    def set_pending_after_review(self, run_id: str, token: int) -> bool:
        with self._session() as connection:
            cursor = connection.execute(
                "UPDATE collab_runs SET state='pending', updated_at=? "
                "WHERE run_id=? AND state='needs_review' AND fencing_token=?",
                (self._clock(), run_id, int(token)),
            )

            return cursor.rowcount == 1

    def commit_step_artifact(
        self,
        run_id: str,
        revision: int,
        step_id: str,
        token: int,
        *,
        staging_path: str,
        target_path: str,
        manifest: dict,
        before_replace=None,
    ) -> bool:
        """条件原子提交：事务内复核 fencing，再 rename 并更新 manifest。

        失败时回滚并删除已移动的目标文件。
        """

        import os as _os

        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        moved = False
        backup = None

        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT fencing_token, revision, state FROM collab_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()

            if (
                row is None
                or int(row["fencing_token"]) != int(token)
                or int(row["revision"]) != int(revision)
                or row["state"] != "running"
            ):
                connection.execute("ROLLBACK")
                return False

            if before_replace is not None:
                before_replace()

            Path(target_path).parent.mkdir(parents=True, exist_ok=True)

            if Path(target_path).exists():
                backup = str(target_path) + f".bak-{int(token)}"
                _os.replace(target_path, backup)

            _os.replace(staging_path, target_path)
            moved = True

            import hashlib as _hashlib

            with open(target_path, "rb") as handle:
                committed_data = handle.read()

            if (
                len(committed_data) != int(manifest.get("bytes", -1))
                or _hashlib.sha256(committed_data).hexdigest() != manifest.get("sha256")
            ):
                raise StoreError("artifact_hash_mismatch")

            cursor = connection.execute(
                "UPDATE collab_steps SET state='done', artifact_manifest=?, updated_at=? "
                "WHERE run_id=? AND revision=? AND step_id=? AND fencing_token=?",
                (
                    json.dumps(manifest, ensure_ascii=False),
                    self._clock(),
                    run_id,
                    revision,
                    step_id,
                    int(token),
                ),
            )

            if cursor.rowcount != 1:
                raise StoreError("stale_worker")

            connection.execute(
                "UPDATE collab_runs SET updated_at=? WHERE run_id=? AND fencing_token=?",
                (self._clock(), run_id, int(token)),
            )
            connection.execute("COMMIT")

            if backup:
                try:
                    _os.remove(backup)
                except OSError:
                    pass

            return True

        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass

            if moved:
                try:
                    _os.remove(target_path)
                except OSError:
                    pass

            if backup:
                try:
                    _os.replace(backup, target_path)
                except OSError:
                    pass

            return False

        finally:
            connection.close()

    def cleanup_orphans(
        self,
        artifact_root: str | Path,
        *,
        staging_max_age: float = 3600.0,
        orphan_min_age: float = 3600.0,
    ) -> dict:
        """启动清理：无 manifest 引用的产物 + 过期 staging。

        每个候选文件在 BEGIN IMMEDIATE 事务内**重新查询引用**后删除，
        与 commit_step_artifact 的写事务串行，避免删掉刚提交的产物。
        """

        import shutil
        import time as _time

        root = Path(artifact_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        removed: list[str] = []

        candidates = [
            path
            for path in root.rglob("*")
            if path.is_file()
            and ".staging" not in path.parts
            and ".bak-" not in path.name
        ]

        for path in candidates:
            try:
                if _time.time() - path.stat().st_mtime < orphan_min_age:
                    continue
            except OSError:
                continue

            relay = str(path.relative_to(root)).replace("\\", "/")
            connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            connection.row_factory = sqlite3.Row

            try:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT artifact_manifest FROM collab_steps WHERE artifact_manifest IS NOT NULL"
                ).fetchall()
                still_referenced = False

                for row in rows:
                    try:
                        manifest = json.loads(row["artifact_manifest"])
                    except ValueError:
                        continue

                    if manifest.get("path") == relay:
                        still_referenced = True
                        break

                if not still_referenced:
                    try:
                        path.unlink()
                        removed.append(relay)
                    except OSError:
                        pass

                connection.execute("COMMIT")
            except sqlite3.Error:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            finally:
                connection.close()

        staging = root / ".staging"

        if staging.exists():
            now = _time.time()

            for entry in staging.iterdir():
                try:
                    if entry.is_dir() and now - entry.stat().st_mtime > staging_max_age:
                        shutil.rmtree(entry, ignore_errors=True)
                        removed.append(f".staging/{entry.name}")
                except OSError:
                    pass

        return {"removed": removed}


    @staticmethod
    def _run_owned(connection, run_id: str, revision: int, token: int, states: tuple[str, ...]) -> bool:
        row = connection.execute(
            "SELECT fencing_token, revision, state FROM collab_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()

        return bool(
            row
            and int(row["fencing_token"]) == int(token)
            and int(row["revision"]) == int(revision)
            and row["state"] in states
        )

    def step_failed(self, run_id: str, revision: int, step_id: str, token: int) -> bool:
        with self._session() as connection:
            if not self._run_owned(connection, run_id, revision, token, ("running",)):
                return False

            cursor = connection.execute(
                "UPDATE collab_steps SET state='failed', updated_at=? "
                "WHERE run_id=? AND revision=? AND step_id=? AND fencing_token=?",
                (self._clock(), run_id, revision, step_id, int(token)),
            )

            return cursor.rowcount == 1

    def step_accept(self, run_id: str, revision: int, step_id: str, token: int, manifest: dict | None) -> bool:
        with self._session() as connection:
            if not self._run_owned(connection, run_id, revision, token, ("running", "needs_review")):
                return False

            cursor = connection.execute(
                "UPDATE collab_steps SET state='done', artifact_manifest=?, updated_at=? "
                "WHERE run_id=? AND revision=? AND step_id=? AND state='needs_review' AND fencing_token=?",
                (
                    json.dumps(manifest, ensure_ascii=False) if manifest else None,
                    self._clock(),
                    run_id,
                    revision,
                    step_id,
                    int(token),
                ),
            )

            return cursor.rowcount == 1

    def step_retry(self, run_id: str, revision: int, step_id: str, token: int) -> bool:
        with self._session() as connection:
            if not self._run_owned(connection, run_id, revision, token, ("running", "needs_review")):
                return False

            cursor = connection.execute(
                "UPDATE collab_steps SET state='pending', updated_at=? "
                "WHERE run_id=? AND revision=? AND step_id=? AND state='needs_review' AND fencing_token=?",
                (self._clock(), run_id, revision, step_id, int(token)),
            )

            return cursor.rowcount == 1

    def step_state(self, run_id: str, revision: int, step_id: str) -> str | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT state FROM collab_steps WHERE run_id=? AND revision=? AND step_id=?",
                (run_id, revision, step_id),
            ).fetchone()

            return row["state"] if row else None

    # ---------- 尝试 ----------

    def attempt_start(self, run_id: str, revision: int, attempt_no: int, provider: str, token: int, *, injected: bool = False) -> str:
        attempt_id = uuid.uuid4().hex

        with self._session() as connection:
            run_row = connection.execute(
                "SELECT fencing_token, revision, state FROM collab_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()

            if (
                run_row is None
                or int(run_row["fencing_token"]) != int(token)
                or int(run_row["revision"]) != int(revision)
                or run_row["state"] != "running"
            ):
                raise StaleWorker("attempt_start")

            connection.execute(
                "INSERT INTO collab_attempts (attempt_id, run_id, revision, attempt_no, provider, "
                "state, injected, fencing_token, started_at) VALUES (?, ?, ?, ?, ?, 'started', ?, ?, ?)",
                (
                    attempt_id,
                    run_id,
                    revision,
                    attempt_no,
                    provider,
                    1 if injected else 0,
                    int(token),
                    self._clock(),
                ),
            )
            cursor = connection.execute(
                "UPDATE collab_runs SET attempts=attempts + 1, updated_at=? WHERE run_id=? AND fencing_token=?",
                (self._clock(), run_id, int(token)),
            )

            if cursor.rowcount != 1:
                raise StaleWorker("attempt_start")

        return attempt_id

    def attempt_finish(self, attempt_id: str, state: str, *, token: int, error_class=None, error=None) -> bool:
        with self._session() as connection:
            cursor = connection.execute(
                "UPDATE collab_attempts SET state=?, error_class=?, error=?, finished_at=? "
                "WHERE attempt_id=? AND fencing_token=?",
                (state, error_class, error, self._clock(), attempt_id, int(token)),
            )

            return cursor.rowcount == 1

    def attempts(self, run_id: str) -> list[dict]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT * FROM collab_attempts WHERE run_id=? ORDER BY started_at",
                (run_id,),
            ).fetchall()

            return [dict(row) for row in rows]
