"""聊天室存储层（S4）：房间、房间成员、消息。

对应 `docs/CHATROOM_PLAN.md` 的 S4（聊天室）。

关键约束：
1. **归属隔离**：房间属于 owner，只有 owner 能拉 Bot 进房、写消息；
2. **成员校验**：拉进房的 Bot 必须是「站长 Bot 或自己的 Bot」且 enabled；
3. **消息按 turn_id 归组**：一轮讨论的所有消息共享同一 turn_id。

与 `BotStore` 共用同一数据库：`room_bots.bot_id` / `messages.bot_id`
外键引用 `bots.id`。
"""

import json
import sqlite3
import time

from contextlib import closing
from pathlib import Path

from src.config import paths
from src.core.bot_store import BotStore


SPEAKERS = ("user", "bot", "host")
ROLES = ("core", "expand", "object", "summary", "decision")

# 辩论流程：顺序就是推进顺序
STAGES = (
    "opening",    # 主持人开场
    "statement",  # 双方立论
    "rebuttal",   # 双方驳论
    "crossfire",  # 轮流质询
    "free",       # 自由交锋
    "closing",    # 双方结辩
    "vote",       # 评委投票
    "review",     # 复盘
)

STAGE_LABELS = {
    "opening": "开场",
    "statement": "立论",
    "rebuttal": "驳论",
    "crossfire": "质询",
    "free": "自由交锋",
    "closing": "结辩",
    "vote": "投票",
    "review": "复盘",
}


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
                    stances TEXT,
                    host_bot_id INTEGER,
                    stage TEXT NOT NULL DEFAULT 'opening',
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                )
                """
            )

            # 迁移：老库缺的列补上。
            room_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(rooms)")
            }

            if "stances" not in room_columns:
                conn.execute(
                    "ALTER TABLE rooms ADD COLUMN stances TEXT"
                )

            if "host_bot_id" not in room_columns:
                conn.execute(
                    "ALTER TABLE rooms ADD COLUMN host_bot_id INTEGER"
                )

            if "stage" not in room_columns:
                conn.execute(
                    "ALTER TABLE rooms ADD COLUMN stage TEXT "
                    "NOT NULL DEFAULT 'opening'"
                )

            if "stage_started_at" not in room_columns:
                conn.execute(
                    "ALTER TABLE rooms "
                    "ADD COLUMN stage_started_at INTEGER"
                )

            if "archived" not in room_columns:
                conn.execute(
                    "ALTER TABLE rooms "
                    "ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
                )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS room_bots (
                    room_id INTEGER NOT NULL
                        REFERENCES rooms(id) ON DELETE CASCADE,
                    bot_id INTEGER NOT NULL
                        REFERENCES bots(id) ON DELETE CASCADE,
                    stance TEXT,
                    activity INTEGER NOT NULL DEFAULT 100,
                    joined_at INTEGER NOT NULL,
                    PRIMARY KEY (room_id, bot_id)
                )
                """
            )

            # 迁移：老库的 room_bots 没有 stance 列。
            rb_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(room_bots)"
                )
            }

            if "stance" not in rb_columns:
                conn.execute(
                    "ALTER TABLE room_bots ADD COLUMN stance TEXT"
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
                    stage TEXT,
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )

            # 迁移：老消息没有 stage 列。
            msg_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(messages)")
            }

            if "stage" not in msg_columns:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN stage TEXT"
                )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS room_votes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id INTEGER NOT NULL
                        REFERENCES rooms(id) ON DELETE CASCADE,
                    voter_id TEXT NOT NULL,
                    stance TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    UNIQUE (room_id, voter_id)
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS room_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id INTEGER NOT NULL
                        REFERENCES rooms(id) ON DELETE CASCADE,
                    bot_id INTEGER NOT NULL
                        REFERENCES bots(id) ON DELETE CASCADE,
                    voter_id TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1,
                    score INTEGER NOT NULL
                        CHECK (score >= 0 AND score <= 10),
                    created_at INTEGER NOT NULL,
                    UNIQUE (room_id, bot_id, voter_id)
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
        stances: list[str] | None = None,
        host_bot_id: int | None = None,
        now: int | None = None,
    ) -> dict:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise RoomError("owner_id 不能为空")

        owner_id = owner_id.strip()
        name = self._name(name)
        now = now or int(time.time())

        if host_bot_id is not None and (
            type(host_bot_id) is not int or host_bot_id <= 0
        ):
            raise RoomError("无效的主持人 ID")

        # 立场（房间的议题阵营）：去空、去重、限长。
        cleaned = []

        for stance in stances or []:
            text = str(stance).strip()

            if text and text not in cleaned:
                cleaned.append(text[:100])

        encoded = (
            json.dumps(cleaned, ensure_ascii=False)
            if cleaned
            else None
        )

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO rooms (
                    owner_id, name, stances,
                    host_bot_id, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (owner_id, name, encoded, host_bot_id, now),
            )

            room_id = cursor.lastrowid

            # 主持人默认进房（它不占立场：是裁判，不是辩手）
            if host_bot_id is not None:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO room_bots (
                        room_id, bot_id, activity, joined_at
                    )
                    VALUES (?, ?, 100, ?)
                    """,
                    (room_id, host_bot_id, now),
                )

        return {
            "id": room_id,
            "owner_id": owner_id,
            "name": name,
            "stances": cleaned,
            "host_bot_id": host_bot_id,
            "stage": STAGES[0],
        }

    def add_bot_to_room(
        self,
        *,
        room_id: int,
        bot_id: int,
        owner_id: str,
        stance: str | None = None,
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

                cleaned = (
                    str(stance).strip()[:100]
                    if stance is not None
                    else ""
                )

                conn.execute(
                    """
                    INSERT OR IGNORE INTO room_bots (
                        room_id, bot_id, stance,
                        activity, joined_at
                    )
                    VALUES (?, ?, ?, 100, ?)
                    """,
                    (
                        room_id,
                        bot_id,
                        cleaned or None,
                        now,
                    ),
                )

                conn.commit()

            except Exception:
                conn.rollback()
                raise

        return True

    # ----------------------------------------------------------------
    # 读取
    # ----------------------------------------------------------------

    def list_rooms(
        self,
        owner_id: str,
        *,
        include_archived: bool = False,
    ) -> list[dict]:
        owner_id = owner_id.strip()
        clause = "" if include_archived else "AND r.archived = 0"

        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT r.id, r.name, r.stances, r.host_bot_id,
                       r.stage, r.stage_started_at, r.archived,
                       r.created_at,
                       COUNT(rb.bot_id) AS bot_count
                FROM rooms r
                LEFT JOIN room_bots rb
                       ON rb.room_id = r.id
                WHERE r.owner_id = ?
                  {clause}
                GROUP BY r.id
                ORDER BY r.created_at DESC, r.id DESC
                """,
                (owner_id,),
            ).fetchall()

        result = []

        for row in rows:
            item = dict(row)
            item["stances"] = (
                json.loads(item["stances"]) if item["stances"] else []
            )
            result.append(item)

        return result

    def submit_score(
        self,
        *,
        room_id: int,
        bot_id: int,
        voter_id: str,
        score: int,
        weight: float = 1.0,
    ) -> dict:
        """给某个辩手打分（同一个人重复提交就覆盖）。

        权重留给多人场景：房主 1.5 票，其他人类 1 票。
        """

        if type(room_id) is not int or room_id <= 0:
            raise RoomError("无效的房间 ID")

        if type(bot_id) is not int or bot_id <= 0:
            raise RoomError("无效的 Bot ID")

        if type(score) is not int or not (0 <= score <= 10):
            raise RoomError("分数必须是 0-10 的整数")

        voter_id = voter_id.strip()
        now = int(time.time())

        with closing(self._connect()) as conn, conn:
            room = conn.execute(
                """
                SELECT id FROM rooms
                WHERE id = ? AND owner_id = ?
                """,
                (room_id, voter_id),
            ).fetchone()

            if room is None:
                raise RoomError("房间不存在或无权访问")

            conn.execute(
                """
                INSERT INTO room_scores (
                    room_id, bot_id, voter_id,
                    weight, score, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (room_id, bot_id, voter_id)
                DO UPDATE SET
                    score = excluded.score,
                    weight = excluded.weight
                """,
                (room_id, bot_id, voter_id, weight, score, now),
            )

        return {"bot_id": bot_id, "score": score, "weight": weight}

    def list_scores(self, *, room_id: int, owner_id: str) -> list[dict]:
        """房间的加权评分（按 Bot 汇总）。"""

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
                SELECT bot_id,
                       ROUND(
                           SUM(score * weight) / SUM(weight), 2
                       ) AS weighted,
                       COUNT(*) AS votes
                FROM room_scores
                WHERE room_id = ?
                GROUP BY bot_id
                """,
                (room_id,),
            ).fetchall()

        return [
            {
                "bot_id": row[0],
                "weighted": row[1],
                "votes": row[2],
            }
            for row in rows
        ]

    def submit_vote(
        self,
        *,
        room_id: int,
        voter_id: str,
        stance: str,
        weight: float = 1.0,
    ) -> dict:
        """评委投票（选一边立场，重复提交覆盖）。

        权重：房主 1.5 票，其他人类 1 票（多人场景预留）。
        """

        if type(room_id) is not int or room_id <= 0:
            raise RoomError("无效的房间 ID")

        stance = str(stance).strip()

        if not stance:
            raise RoomError("立场不能为空")

        voter_id = voter_id.strip()
        now = int(time.time())

        with closing(self._connect()) as conn, conn:
            room = conn.execute(
                """
                SELECT id FROM rooms
                WHERE id = ? AND owner_id = ?
                """,
                (room_id, voter_id),
            ).fetchone()

            if room is None:
                raise RoomError("房间不存在或无权访问")

            conn.execute(
                """
                INSERT INTO room_votes (
                    room_id, voter_id, stance, weight, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (room_id, voter_id)
                DO UPDATE SET
                    stance = excluded.stance,
                    weight = excluded.weight
                """,
                (room_id, voter_id, stance, weight, now),
            )

        return {
            "room_id": room_id,
            "voter_id": voter_id,
            "stance": stance,
            "weight": weight,
        }

    def list_votes(self, *, room_id: int, owner_id: str) -> dict:
        """投票汇总：每个立场拿了多少票（加权）。"""

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
                SELECT stance, SUM(weight) AS weighted,
                       COUNT(*) AS votes
                FROM room_votes
                WHERE room_id = ?
                GROUP BY stance
                """,
                (room_id,),
            ).fetchall()

        return {
            "tally": [
                {
                    "stance": row[0],
                    "weighted": round(row[1], 2),
                    "votes": row[2],
                }
                for row in rows
            ]
        }

    def advance_stage(
        self,
        *,
        room_id: int,
        owner_id: str,
        stage: str,
    ) -> bool:
        """把房间推进到指定阶段（不能倒退，防止流程乱跳）。"""

        if stage not in STAGES:
            raise RoomError("未知阶段")

        if type(room_id) is not int or room_id <= 0:
            raise RoomError("无效的房间 ID")

        owner_id = owner_id.strip()

        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                """
                SELECT stage FROM rooms
                WHERE id = ? AND owner_id = ?
                """,
                (room_id, owner_id),
            ).fetchone()

            if row is None:
                raise RoomError("房间不存在或无权访问")

            current = row[0] or STAGES[0]

            # 只许往前走
            if STAGES.index(stage) <= STAGES.index(current):
                return False

            conn.execute(
                """
                UPDATE rooms
                SET stage = ?, stage_started_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (stage, int(time.time()), room_id, owner_id),
            )

        return True

    def get_room(
        self, *, room_id: int, owner_id: str
    ) -> dict | None:
        """取单个房间（含议题立场）。"""

        if type(room_id) is not int or room_id <= 0:
            raise RoomError("无效的房间 ID")

        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT id, name, stances, host_bot_id,
                       stage, stage_started_at,
                       archived, created_at
                FROM rooms
                WHERE id = ? AND owner_id = ?
                """,
                (room_id, owner_id.strip()),
            ).fetchone()

        if row is None:
            return None

        item = dict(row)
        item["stances"] = (
            json.loads(item["stances"]) if item["stances"] else []
        )

        return item

    def archive_room(
        self,
        *,
        room_id: int,
        owner_id: str,
        archived: bool = True,
    ) -> bool:
        """归档 / 取消归档房间（软隐藏，不删数据）。"""

        if type(room_id) is not int or room_id <= 0:
            raise RoomError("无效的房间 ID")

        owner_id = owner_id.strip()

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                UPDATE rooms SET archived = ?
                WHERE id = ? AND owner_id = ?
                """,
                (1 if archived else 0, room_id, owner_id),
            )

        return cursor.rowcount == 1

    def apply_stance_penalty(
        self,
        *,
        room_id: int,
        owner_id: str,
        chosen_stance: str | None,
        penalty: int = 30,
    ) -> list[dict]:
        """用户拍板后结算活跃值（S6 后手）。

        规则：**站对边的不扣**（它判断对了），其余一律扣 penalty，
        扣到 0 为止。返回被扣分的成员（含扣后活跃值）。

        `chosen_stance` 为空表示没有立场可对，全场一起扣。
        """

        if type(room_id) is not int or room_id <= 0:
            raise RoomError("无效的房间 ID")

        if type(penalty) is not int or penalty < 0:
            raise RoomError("扣分必须是自然数")

        owner_id = owner_id.strip()

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

            if chosen_stance:
                rows = conn.execute(
                    """
                    UPDATE room_bots
                    SET activity = MAX(0, activity - ?)
                    WHERE room_id = ?
                      AND (stance IS NULL OR stance != ?)
                    RETURNING bot_id, activity
                    """,
                    (penalty, room_id, chosen_stance),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    UPDATE room_bots
                    SET activity = MAX(0, activity - ?)
                    WHERE room_id = ?
                    RETURNING bot_id, activity
                    """,
                    (penalty, room_id),
                ).fetchall()

        return [
            {"bot_id": row[0], "activity": row[1]} for row in rows
        ]

    def delete_room(self, *, room_id: int, owner_id: str) -> bool:
        """删除房间（级联清掉成员 / 消息 / 举手）。"""

        if type(room_id) is not int or room_id <= 0:
            raise RoomError("无效的房间 ID")

        owner_id = owner_id.strip()

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                DELETE FROM rooms
                WHERE id = ? AND owner_id = ?
                """,
                (room_id, owner_id),
            )

        return cursor.rowcount == 1

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
                       b.system_prompt, b.avatar, b.voice,
                       rb.stance, rb.activity
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
        stage: str | None = None,
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
                    bot_id, role, stage, content, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room_id,
                    turn_id.strip(),
                    speaker,
                    bot_id,
                    role,
                    stage,
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
            "stage": stage,
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
                           role, stage, content, created_at
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
                           role, stage, content, created_at
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
