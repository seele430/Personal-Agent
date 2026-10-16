"""聊天室存储层（S4）：房间、房间成员、消息。

对应 `docs/CHATROOM_PLAN.md` 的 S4（聊天室）。

关键约束：
1. **归属隔离**：房间属于 owner，只有 owner 能拉 Bot 进房、写消息；
2. **成员校验**：拉进房的 Bot 必须是「站长 Bot 或自己的 Bot」且 enabled；
3. **消息按 turn_id 归组**：一轮讨论的所有消息共享同一 turn_id。

与 `BotStore` 共用同一数据库：`room_bots.bot_id` / `messages.bot_id`
外键引用 `bots.id`。
"""

import sqlite3
import time

from contextlib import closing
from pathlib import Path

from src.config import paths
from src.core.bot_store import BotStore


SPEAKERS = ("user", "bot", "host")
ROLES = ("core", "expand", "object", "summary", "decision")


class RoomError(ValueError):
    """聊天室域的业务错误。"""


class RoomStore:

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or paths.bots_db)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # 确保 bots 表存在（外键引用）。
        BotStore(self.db_path)

        self.init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self):
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rooms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS room_bots (
                    room_id INTEGER NOT NULL
                        REFERENCES rooms(id) ON DELETE CASCADE,
                    bot_id INTEGER NOT NULL
                        REFERENCES bots(id) ON DELETE CASCADE,
                    activity INTEGER NOT NULL DEFAULT 100,
                    joined_at INTEGER NOT NULL,
                    PRIMARY KEY (room_id, bot_id)
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id INTEGER NOT NULL
                        REFERENCES rooms(id) ON DELETE CASCADE,
                    turn_id TEXT NOT NULL,
                    speaker TEXT NOT NULL
                        CHECK (speaker IN ('user', 'bot', 'host')),
                    bot_id INTEGER
                        REFERENCES bots(id) ON DELETE SET NULL,
                    role TEXT NOT NULL
                        CHECK (role IN
                               ('core', 'expand', 'object',
                                'summary', 'decision')),
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id INTEGER NOT NULL
                        REFERENCES rooms(id) ON DELETE CASCADE,
                    turn_id TEXT NOT NULL,
                    bot_id INTEGER NOT NULL
                        REFERENCES bots(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL
                        CHECK (kind IN ('expand', 'object')),
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN
                               ('pending', 'approved', 'rejected')),
                    created_at INTEGER NOT NULL
                )
                """
            )

    # ----------------------------------------------------------------
    # 校验
    # ----------------------------------------------------------------

    @staticmethod
    def _name(value):
        if not isinstance(value, str) or not value.strip():
            raise RoomError("房间名不能为空")

        value = value.strip()

        if len(value) > 50:
            raise RoomError("房间名过长（上限 50 字）")

        return value

    # ----------------------------------------------------------------
    # 写入
    # ----------------------------------------------------------------

    def create_room(
        self,
        *,
        owner_id: str,
        name: str,
        now: int | None = None,
    ) -> dict:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise RoomError("owner_id 不能为空")

        owner_id = owner_id.strip()
        name = self._name(name)
        now = now or int(time.time())

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO rooms (owner_id, name, created_at)
                VALUES (?, ?, ?)
                """,
                (owner_id, name, now),
            )

        return {
            "id": cursor.lastrowid,
            "owner_id": owner_id,
            "name": name,
        }

    def add_bot_to_room(
        self,
        *,
        room_id: int,
        bot_id: int,
        owner_id: str,
        now: int | None = None,
    ) -> bool:
        """把 Bot 拉进房（幂等）。校验房间归属 + Bot 可见性。"""

        if type(room_id) is not int or room_id <= 0:
            raise RoomError("无效的房间 ID")

        if type(bot_id) is not int or bot_id <= 0:
            raise RoomError("无效的 Bot ID")

        owner_id = owner_id.strip()
        now = now or int(time.time())

        with closing(self._connect()) as conn:

            try:
                conn.execute("BEGIN IMMEDIATE")

                room = conn.execute(
                    """
                    SELECT id FROM rooms
                    WHERE id = ? AND owner_id = ?
                    """,
                    (room_id, owner_id),
                ).fetchone()

                if room is None:
                    raise RoomError("房间不存在或无权访问")

                bot = conn.execute(
                    """
                    SELECT id FROM bots
                    WHERE id = ?
                      AND enabled = 1
                      AND (owner_id IS NULL OR owner_id = ?)
                    """,
                    (bot_id, owner_id),
                ).fetchone()

                if bot is None:
                    raise RoomError("Bot 不存在或无权访问")

                conn.execute(
                    """
                    INSERT OR IGNORE INTO room_bots (
                        room_id, bot_id, activity, joined_at
                    )
                    VALUES (?, ?, 100, ?)
                    """,
                    (room_id, bot_id, now),
                )

                conn.commit()

            except Exception:
                conn.rollback()
                raise

        return True

    # ----------------------------------------------------------------
    # 读取
    # ----------------------------------------------------------------

    def list_rooms(self, owner_id: str) -> list[dict]:
        owner_id = owner_id.strip()

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT r.id, r.name, r.created_at,
                       COUNT(rb.bot_id) AS bot_count
                FROM rooms r
                LEFT JOIN room_bots rb
                       ON rb.room_id = r.id
                WHERE r.owner_id = ?
                GROUP BY r.id
                ORDER BY r.id DESC
                """,
                (owner_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def list_room_bots(
        self,
        *,
        room_id: int,
        owner_id: str,
    ) -> list[dict]:
        owner_id = owner_id.strip()

        with closing(self._connect()) as conn:
            room = conn.execute(
                """
                SELECT id FROM rooms
                WHERE id = ? AND owner_id = ?
                """,
                (room_id, owner_id),
            ).fetchone()

            if room is None:
                raise RoomError("房间不存在或无权访问")

            rows = conn.execute(
                """
                SELECT b.id, b.name, b.persona,
                       b.avatar, rb.activity
                FROM room_bots rb
                JOIN bots b ON b.id = rb.bot_id
                WHERE rb.room_id = ?
                ORDER BY rb.joined_at
                """,
                (room_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    # ----------------------------------------------------------------
    # 消息
    # ----------------------------------------------------------------

    def add_message(
        self,
        *,
        room_id: int,
        turn_id: str,
        speaker: str,
        bot_id: int | None,
        role: str,
        content: str,
        owner_id: str,
        now: int | None = None,
    ) -> dict:
        if type(room_id) is not int or room_id <= 0:
            raise RoomError("无效的房间 ID")

        if speaker not in SPEAKERS:
            raise RoomError("speaker 无效")

        if role not in ROLES:
            raise RoomError("role 无效")

        if not isinstance(content, str) or not content.strip():
            raise RoomError("消息内容不能为空")

        if not isinstance(turn_id, str) or not turn_id.strip():
            raise RoomError("turn_id 不能为空")

        owner_id = owner_id.strip()
        now = now or int(time.time())

        with closing(self._connect()) as conn, conn:
            room = conn.execute(
                """
                SELECT id FROM rooms
                WHERE id = ? AND owner_id = ?
                """,
                (room_id, owner_id),
            ).fetchone()

            if room is None:
                raise RoomError("房间不存在或无权访问")

            cursor = conn.execute(
                """
                INSERT INTO messages (
                    room_id, turn_id, speaker,
                    bot_id, role, content, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room_id,
                    turn_id.strip(),
                    speaker,
                    bot_id,
                    role,
                    content.strip(),
                    now,
                ),
            )

        return {
            "id": cursor.lastrowid,
            "room_id": room_id,
            "turn_id": turn_id,
            "speaker": speaker,
            "role": role,
        }

    def list_messages(
        self,
        *,
        room_id: int,
        owner_id: str,
        turn_id: str | None = None,
    ) -> list[dict]:
        owner_id = owner_id.strip()

        with closing(self._connect()) as conn:
            room = conn.execute(
                """
                SELECT id FROM rooms
                WHERE id = ? AND owner_id = ?
                """,
                (room_id, owner_id),
            ).fetchone()

            if room is None:
                raise RoomError("房间不存在或无权访问")

            if turn_id is None:
                rows = conn.execute(
                    """
                    SELECT id, turn_id, speaker, bot_id,
                           role, content, created_at
                    FROM messages
                    WHERE room_id = ?
                    ORDER BY id
                    """,
                    (room_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, turn_id, speaker, bot_id,
                           role, content, created_at
                    FROM messages
                    WHERE room_id = ? AND turn_id = ?
                    ORDER BY id
                    """,
                    (room_id, turn_id),
                ).fetchall()

        return [dict(row) for row in rows]

    # ----------------------------------------------------------------
    # 举手（S5）
    # ----------------------------------------------------------------

    HAND_KINDS = ("expand", "object")
    ACTIVITY_PENALTY = 20

    def raise_hand(
        self,
        *,
        room_id: int,
        turn_id: str,
        bot_id: int,
        kind: str,
        reason: str,
        owner_id: str,
        now: int | None = None,
    ) -> dict:
        """Bot 举手（想展开 / 想追问），等用户批准。"""

        if type(room_id) is not int or room_id <= 0:
            raise RoomError("无效的房间 ID")

        if type(bot_id) is not int or bot_id <= 0:
            raise RoomError("无效的 Bot ID")

        if kind not in self.HAND_KINDS:
            raise RoomError("kind 无效")

        if not isinstance(reason, str) or not reason.strip():
            raise RoomError("举手理由不能为空")

        if not isinstance(turn_id, str) or not turn_id.strip():
            raise RoomError("turn_id 不能为空")

        owner_id = owner_id.strip()
        now = now or int(time.time())

        with closing(self._connect()) as conn, conn:
            room = conn.execute(
                """
                SELECT id FROM rooms
                WHERE id = ? AND owner_id = ?
                """,
                (room_id, owner_id),
            ).fetchone()

            if room is None:
                raise RoomError("房间不存在或无权访问")

            member = conn.execute(
                """
                SELECT 1 FROM room_bots
                WHERE room_id = ? AND bot_id = ?
                """,
                (room_id, bot_id),
            ).fetchone()

            if member is None:
                raise RoomError("Bot 不在房间里")

            cursor = conn.execute(
                """
                INSERT INTO hands (
                    room_id, turn_id, bot_id,
                    kind, reason, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    room_id,
                    turn_id.strip(),
                    bot_id,
                    kind,
                    reason.strip(),
                    now,
                ),
            )

        return {
            "id": cursor.lastrowid,
            "room_id": room_id,
            "turn_id": turn_id.strip(),
            "bot_id": bot_id,
            "kind": kind,
            "status": "pending",
        }

    def approve_hand(
        self,
        *,
        hand_id: int,
        owner_id: str,
    ) -> bool:
        """批准举手（pending → approved）。"""

        if type(hand_id) is not int or hand_id <= 0:
            raise RoomError("无效的举手 ID")

        owner_id = owner_id.strip()

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                UPDATE hands
                SET status = 'approved'
                WHERE id = ?
                  AND status = 'pending'
                  AND room_id IN (
                      SELECT id FROM rooms WHERE owner_id = ?
                  )
                """,
                (hand_id, owner_id),
            )

        return cursor.rowcount == 1

    def get_hand(
        self,
        *,
        hand_id: int,
        owner_id: str,
    ) -> dict | None:
        """按 hand_id 查举手记录（含 room_id/bot_id/turn_id/kind）。"""

        if type(hand_id) is not int or hand_id <= 0:
            raise RoomError("无效的举手 ID")

        owner_id = owner_id.strip()

        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT h.id, h.room_id, h.turn_id,
                       h.bot_id, h.kind, h.reason, h.status
                FROM hands h
                JOIN rooms r ON r.id = h.room_id
                WHERE h.id = ? AND r.owner_id = ?
                """,
                (hand_id, owner_id),
            ).fetchone()

        return dict(row) if row else None

    def reject_hand(
        self,
        *,
        hand_id: int,
        owner_id: str,
        unfair: bool = False,
    ) -> dict:
        """拒绝举手（pending → rejected）；unfair 时扣该 Bot 活跃度。"""

        if type(hand_id) is not int or hand_id <= 0:
            raise RoomError("无效的举手 ID")

        owner_id = owner_id.strip()

        with closing(self._connect()) as conn:

            try:
                conn.execute("BEGIN IMMEDIATE")

                hand = conn.execute(
                    """
                    SELECT h.id, h.room_id, h.bot_id
                    FROM hands h
                    JOIN rooms r ON r.id = h.room_id
                    WHERE h.id = ? AND r.owner_id = ?
                    """,
                    (hand_id, owner_id),
                ).fetchone()

                if hand is None:
                    conn.rollback()
                    return {"changed": False, "unfair": unfair}

                updated = conn.execute(
                    """
                    UPDATE hands
                    SET status = 'rejected'
                    WHERE id = ? AND status = 'pending'
                    """,
                    (hand_id,),
                ).rowcount

                if updated and unfair:
                    conn.execute(
                        """
                        UPDATE room_bots
                        SET activity = MAX(0, activity - ?)
                        WHERE room_id = ? AND bot_id = ?
                        """,
                        (
                            self.ACTIVITY_PENALTY,
                            hand["room_id"],
                            hand["bot_id"],
                        ),
                    )

                conn.commit()

            except Exception:
                conn.rollback()
                raise

        return {"changed": True, "unfair": unfair}

    def list_hands(
        self,
        *,
        room_id: int,
        owner_id: str,
        status: str | None = None,
    ) -> list[dict]:
        owner_id = owner_id.strip()

        with closing(self._connect()) as conn:
            room = conn.execute(
                """
                SELECT id FROM rooms
                WHERE id = ? AND owner_id = ?
                """,
                (room_id, owner_id),
            ).fetchone()

            if room is None:
                raise RoomError("房间不存在或无权访问")

            if status is None:
                rows = conn.execute(
                    """
                    SELECT id, turn_id, bot_id, kind,
                           reason, status, created_at
                    FROM hands
                    WHERE room_id = ?
                    ORDER BY id
                    """,
                    (room_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, turn_id, bot_id, kind,
                           reason, status, created_at
                    FROM hands
                    WHERE room_id = ? AND status = ?
                    ORDER BY id
                    """,
                    (room_id, status),
                ).fetchall()

        return [dict(row) for row in rows]
