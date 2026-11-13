"""会话存储：网页与 QQ 共用的一套「会话 + 消息」表。

边界：

- 只做本地 SQLite；不做多用户/权限；
- 软删（deleted_at）不物理删除；
- QQ 每次上线由 lifecycle 事件建新会话；`active` 指针记录「当前 QQ 会话」；
- 消息写入靠 `(session_id, message_id, role)` 唯一约束防重复。
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid

from contextlib import contextmanager
from pathlib import Path


MAX_TITLE_CHARS = 40
DEFAULT_CONTEXT_MESSAGES = 10
DEFAULT_CONTEXT_CHARS = 4000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL CHECK (source IN ('web','qq')),
    title           TEXT NOT NULL DEFAULT '',
    title_source    TEXT NOT NULL DEFAULT 'auto',
    bot_launch_id   TEXT,
    pinned_at       REAL,
    deleted_at      REAL,
    last_message_at REAL,
    message_count   INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS session_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content    TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE (session_id, message_id, role)
);

CREATE INDEX IF NOT EXISTS idx_session_messages
    ON session_messages(session_id, id);

CREATE TABLE IF NOT EXISTS qq_active_sessions (
    sender     INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class SessionError(ValueError):
    """会话参数不合法。"""


def data_root() -> Path:
    raw = (os.getenv("PERSONAL_AGENT_DATA_DIR") or "").strip()

    if raw:
        return Path(raw)

    return Path(__file__).resolve().parents[1] / "data"


def _row_to_session(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "source": row["source"],
        "title": row["title"],
        "title_source": row["title_source"],
        "bot_launch_id": row["bot_launch_id"],
        "pinned": bool(row["pinned_at"]),
        "deleted": bool(row["deleted_at"]),
        "last_message_at": row["last_message_at"],
        "message_count": int(row["message_count"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class SessionStore:
    def __init__(self, db_path: str | Path | None = None, *, clock=None):
        self.db_path = str(db_path or (data_root() / "sessions.db"))
        self._clock = clock or time.time
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        with self._session() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def _session(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row

        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    # ---------- 会话 ----------

    def create(
        self,
        source: str,
        title: str = "",
        *,
        bot_launch_id: str | None = None,
    ) -> dict:
        if source not in ("web", "qq"):
            raise SessionError("source 只能是 web / qq")

        now = self._clock()
        identifier = str(uuid.uuid4())
        title = (title or "").strip()[:MAX_TITLE_CHARS]

        with self._session() as connection:
            connection.execute(
                "INSERT INTO sessions "
                "(id, source, title, title_source, bot_launch_id, created_at, updated_at) "
                "VALUES (?, ?, ?, 'auto', ?, ?, ?)",
                (identifier, source, title, bot_launch_id, now, now),
            )

        return self.get(identifier)  # type: ignore[return-value]

    def get(self, session_id: str) -> dict | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id=?", (str(session_id),)
            ).fetchone()

        return _row_to_session(row) if row else None

    def list(self, limit: int = 50, *, include_deleted: bool = False) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        where = "" if include_deleted else "WHERE deleted_at IS NULL"

        with self._session() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions " + where +
                " ORDER BY (pinned_at IS NULL), pinned_at DESC, "
                "COALESCE(last_message_at, updated_at) DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return [_row_to_session(row) for row in rows]

    def rename(self, session_id: str, title: str) -> bool:
        title = (title or "").strip()[:MAX_TITLE_CHARS]

        if not title:
            raise SessionError("标题不能为空")

        with self._session() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET title=?, title_source='user', updated_at=? WHERE id=?",
                (title, self._clock(), str(session_id)),
            )

        return cursor.rowcount == 1

    def set_pinned(self, session_id: str, pinned: bool) -> bool:
        with self._session() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET pinned_at=?, updated_at=? WHERE id=?",
                (self._clock() if pinned else None, self._clock(), str(session_id)),
            )

        return cursor.rowcount == 1

    def soft_delete(self, session_id: str) -> bool:
        with self._session() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET deleted_at=?, updated_at=? WHERE id=?",
                (self._clock(), self._clock(), str(session_id)),
            )

        return cursor.rowcount == 1

    def find_by_prefix(self, prefix: str) -> list[dict]:
        prefix = (prefix or "").strip()

        if len(prefix) < 6:
            raise SessionError("前缀至少 6 位")

        with self._session() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions WHERE id LIKE ? AND deleted_at IS NULL "
                "ORDER BY updated_at DESC LIMIT 5",
                (prefix + "%",),
            ).fetchall()

        return [_row_to_session(row) for row in rows]

    # ---------- 消息 ----------

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        message_id: str = "",
    ) -> bool:
        if role not in ("user", "assistant", "system"):
            raise SessionError("role 只能是 user/assistant/system")

        content = (content or "").strip()

        if not content:
            return False

        now = self._clock()

        with self._session() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO session_messages "
                "(session_id, role, content, message_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(session_id), role, content, str(message_id or ""), now),
            )

            if cursor.rowcount != 1:
                return False

            connection.execute(
                "UPDATE sessions SET last_message_at=?, updated_at=?, "
                "message_count=message_count+1 WHERE id=?",
                (now, now, str(session_id)),
            )

            row = connection.execute(
                "SELECT source, title, title_source, message_count FROM sessions WHERE id=?",
                (str(session_id),),
            ).fetchone()

            if row and row["source"] == "qq" and row["title_source"] == "auto" \
                    and role == "user" and not row["title"]:
                title = content[:20]
                connection.execute(
                    "UPDATE sessions SET title=? WHERE id=?",
                    (title, str(session_id)),
                )

        return True

    def messages(self, session_id: str, after: int = 0, limit: int = 200) -> list[dict]:
        limit = max(1, min(int(limit), 500))

        with self._session() as connection:
            rows = connection.execute(
                "SELECT id, role, content, message_id, created_at "
                "FROM session_messages WHERE session_id=? AND id>? "
                "ORDER BY id ASC LIMIT ?",
                (str(session_id), int(after), limit),
            ).fetchall()

        return [dict(row) for row in rows]

    def context(
        self,
        session_id: str,
        *,
        max_messages: int = DEFAULT_CONTEXT_MESSAGES,
        max_chars: int = DEFAULT_CONTEXT_CHARS,
        exclude_message_id: str | None = None,
    ) -> str:
        query = (
            "SELECT role, content FROM session_messages "
            "WHERE session_id=?"
        )
        params: list[object] = [str(session_id)]

        if exclude_message_id:
            query += " AND message_id != ?"
            params.append(str(exclude_message_id))

        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(max_messages))

        with self._session() as connection:
            rows = connection.execute(query, params).fetchall()

        # 从最新消息开始预留预算；超出时丢弃最旧消息。
        pieces: list[str] = []
        used = 0

        for row in rows:
            label = "用户" if row["role"] == "user" else "助手"
            line = f"{label}：{row['content']}"
            separator = 1 if pieces else 0
            remaining = max_chars - used - separator

            if remaining <= 0:
                break

            if len(line) > remaining:
                if not pieces:
                    pieces.append(line[:max(remaining - 1, 0)] + "…")

                break

            pieces.append(line)
            used += len(line) + separator

        return "\n".join(reversed(pieces))

    # ---------- QQ 当前会话 ----------

    def active_session(self, sender: int) -> dict | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT session_id FROM qq_active_sessions WHERE sender=?",
                (int(sender),),
            ).fetchone()

            if row is None:
                return None

            session = connection.execute(
                "SELECT * FROM sessions WHERE id=?", (row["session_id"],)
            ).fetchone()

        return _row_to_session(session) if session else None

    def set_active(self, sender: int, session_id: str) -> None:
        with self._session() as connection:
            connection.execute(
                "INSERT INTO qq_active_sessions (sender, session_id, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(sender) DO UPDATE SET "
                "session_id=excluded.session_id, updated_at=excluded.updated_at",
                (int(sender), str(session_id), self._clock()),
            )

    def ensure_qq_launch(
        self, bot_launch_id: str, *, owner_id: int, title: str = ""
    ) -> dict:
        """同一次上线只建一个会话；重复事件幂等。"""

        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE source='qq' AND bot_launch_id=?",
                (str(bot_launch_id),),
            ).fetchone()

        if row:
            session = _row_to_session(row)
            self.set_active(owner_id, session["id"])

            return session

        session = self.create("qq", title or "QQ · " + self._clock_label(),
                              bot_launch_id=str(bot_launch_id))
        self.set_active(owner_id, session["id"])

        return session

    def _clock_label(self) -> str:
        return time.strftime("%H:%M", time.localtime(self._clock()))

    def ensure_active(self, sender: int, *, source: str = "qq") -> dict:
        """取当前会话；没有就新建一个（兜底，不依赖 lifecycle 事件）。"""

        session = self.active_session(sender)

        if session and not session["deleted"]:
            return session

        session = self.create(source, "")
        self.set_active(sender, session["id"])

        return session
