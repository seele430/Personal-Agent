"""Bot 存储层：身份卡（persona / system_prompt）+ 双来源（platform / byok）。

对应 `docs/CHATROOM_PLAN.md` 的 S1（Bot 实体）。

关键约束：
1. **归属隔离**：用户 Bot 带 `owner_id`，站长 Bot `owner_id` 为 NULL；
   `list_bots` 只返回「站长 Bot + 该用户自己的 Bot」，绝不跨用户可见。
2. **单用户上限**：每个用户最多 3 个自有 Bot（站长 Bot 不计入）。
3. **身份卡是差异化的核心**：`persona` 是天然立场，`system_prompt` 是行为指令；
   即使同一个 API、同一个模型，不同身份卡也产出不同观点，避免发言重合。
"""

import sqlite3
import time

from contextlib import closing
from pathlib import Path

from src.config import paths


MAX_BOTS_PER_USER = 3
API_SOURCES = ("platform", "byok")

# 头像风格标识（key），前端映射到对应的风格化图标。
# 只用风格化/抽象图标，不用各厂商官方 logo 原图（商标版权）。
AVATARS = (
    "default",
    "deepseek",
    "doubao",
    "qwen",
    "kimi",
    "ernie",
    "claude",
    "gpt",
    "gemini",
)


class BotError(ValueError):
    """Bot 域的业务错误。"""


class BotStore:

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or paths.bots_db)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS bots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT,
                    name TEXT NOT NULL,
                    persona TEXT NOT NULL,
                    system_prompt TEXT NOT NULL,
                    expertise TEXT,
                    style TEXT,
                    api_source TEXT NOT NULL
                        CHECK (api_source IN ('platform', 'byok')),
                    avatar TEXT NOT NULL DEFAULT 'default',
                    enabled INTEGER NOT NULL DEFAULT 1
                        CHECK (enabled IN (0, 1)),
                    created_at INTEGER NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bots_owner
                ON bots (owner_id, id)
                """
            )

            # 迁移：老库没有 avatar 列（S1 后续补充）。
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(bots)"
                )
            }

            if "avatar" not in columns:
                conn.execute(
                    "ALTER TABLE bots "
                    "ADD COLUMN avatar TEXT NOT NULL DEFAULT 'default'"
                )

            # 用户默认 Bot（S6）：未设置时回退到第一个站长 Bot。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_defaults (
                    user_id TEXT PRIMARY KEY,
                    default_bot_id INTEGER
                        REFERENCES bots(id) ON DELETE SET NULL
                )
                """
            )

    # ----------------------------------------------------------------
    # 校验
    # ----------------------------------------------------------------

    @staticmethod
    def _text(value, field, *, max_len):
        if not isinstance(value, str) or not value.strip():
            raise BotError(f"{field}不能为空")

        value = value.strip()

        if len(value) > max_len:
            raise BotError(f"{field}过长（上限 {max_len} 字）")

        return value

    @staticmethod
    def _api_source(value):
        if value not in API_SOURCES:
            raise BotError(
                "api_source 必须是 platform 或 byok"
            )
        return value

    @staticmethod
    def _avatar(value):
        if value not in AVATARS:
            raise BotError(
                "avatar 必须是 " + "/".join(AVATARS) + " 之一"
            )
        return value

    # ----------------------------------------------------------------
    # 写入
    # ----------------------------------------------------------------

    def create_bot(
        self,
        *,
        owner_id: str,
        name: str,
        persona: str,
        system_prompt: str,
        api_source: str,
        avatar: str = "default",
        expertise: str | None = None,
        style: str | None = None,
        now: int | None = None,
    ) -> dict:
        """为用户创建自有 Bot；单用户上限 3。"""

        if not isinstance(owner_id, str) or not owner_id.strip():
            raise BotError("owner_id 不能为空")

        owner_id = owner_id.strip()
        name = self._text(name, "name", max_len=50)
        persona = self._text(persona, "persona", max_len=200)
        system_prompt = self._text(
            system_prompt, "system_prompt", max_len=4000
        )
        api_source = self._api_source(api_source)
        avatar = self._avatar(avatar)
        expertise = (
            self._text(expertise, "expertise", max_len=200)
            if expertise
            else None
        )
        style = (
            self._text(style, "style", max_len=100)
            if style
            else None
        )
        now = now or int(time.time())

        with closing(self._connect()) as conn:

            try:
                conn.execute("BEGIN IMMEDIATE")

                owned = conn.execute(
                    """
                    SELECT COUNT(*) FROM bots
                    WHERE owner_id = ?
                    """,
                    (owner_id,),
                ).fetchone()[0]

                if owned >= MAX_BOTS_PER_USER:
                    raise BotError(
                        f"每个用户最多 {MAX_BOTS_PER_USER} 个 Bot"
                    )

                cursor = conn.execute(
                    """
                    INSERT INTO bots (
                        owner_id, name, persona, system_prompt,
                        expertise, style, api_source, avatar, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner_id,
                        name,
                        persona,
                        system_prompt,
                        expertise,
                        style,
                        api_source,
                        avatar,
                        now,
                    ),
                )

                conn.commit()

            except Exception:
                conn.rollback()
                raise

        return {
            "id": cursor.lastrowid,
            "owner_id": owner_id,
            "name": name,
            "persona": persona,
            "system_prompt": system_prompt,
            "expertise": expertise,
            "style": style,
            "api_source": api_source,
            "avatar": avatar,
            "enabled": True,
        }

    def seed_platform_bot(
        self,
        *,
        name: str,
        persona: str,
        system_prompt: str,
        avatar: str = "default",
        expertise: str | None = None,
        style: str | None = None,
        now: int | None = None,
    ) -> dict:
        """创建站长/平台 Bot（owner_id = NULL）。

        仅用于初始化或管理员场景，绝不暴露为普通用户 API。
        """

        name = self._text(name, "name", max_len=50)
        persona = self._text(persona, "persona", max_len=200)
        system_prompt = self._text(
            system_prompt, "system_prompt", max_len=4000
        )
        avatar = self._avatar(avatar)
        expertise = (
            self._text(expertise, "expertise", max_len=200)
            if expertise
            else None
        )
        style = (
            self._text(style, "style", max_len=100)
            if style
            else None
        )
        now = now or int(time.time())

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO bots (
                    owner_id, name, persona, system_prompt,
                    expertise, style, api_source, avatar, created_at
                )
                VALUES (NULL, ?, ?, ?, ?, ?, 'platform', ?, ?)
                """,
                (
                    name,
                    persona,
                    system_prompt,
                    expertise,
                    style,
                    avatar,
                    now,
                ),
            )

        return {
            "id": cursor.lastrowid,
            "owner_id": None,
            "name": name,
            "persona": persona,
            "system_prompt": system_prompt,
            "expertise": expertise,
            "style": style,
            "api_source": "platform",
            "avatar": avatar,
            "enabled": True,
        }

    # ----------------------------------------------------------------
    # 读取
    # ----------------------------------------------------------------

    def list_bots(self, owner_id: str) -> list[dict]:
        """该用户可见的 Bot = 站长 Bot + 该用户自己的 Bot。"""

        if not isinstance(owner_id, str) or not owner_id.strip():
            raise BotError("owner_id 不能为空")

        owner_id = owner_id.strip()

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, owner_id, name, persona, system_prompt,
                       expertise, style, api_source, enabled, created_at
                FROM bots
                WHERE owner_id IS NULL OR owner_id = ?
                ORDER BY (owner_id IS NOT NULL), id
                """,
                (owner_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def get_bot(self, bot_id: int) -> dict | None:
        if type(bot_id) is not int or bot_id <= 0:
            raise BotError("无效的 Bot ID")

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM bots WHERE id = ?",
                (bot_id,),
            ).fetchone()

        return dict(row) if row else None

    def get_visible_bot(
        self,
        *,
        bot_id: int,
        owner_id: str,
    ) -> dict | None:
        """用户可见且启用的 Bot（站长 Bot 或自己的 Bot）。"""

        if type(bot_id) is not int or bot_id <= 0:
            raise BotError("无效的 Bot ID")

        owner_id = owner_id.strip()

        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM bots
                WHERE id = ?
                  AND enabled = 1
                  AND (owner_id IS NULL OR owner_id = ?)
                """,
                (bot_id, owner_id),
            ).fetchone()

        return dict(row) if row else None

    def count_owned(self, owner_id: str) -> int:
        owner_id = owner_id.strip()

        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM bots WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()[0]

    # ----------------------------------------------------------------
    # 更新
    # ----------------------------------------------------------------

    def update_bot(
        self,
        *,
        owner_id: str,
        bot_id: int,
        **fields,
    ) -> bool:
        """更新身份卡字段。只允许 owner 改自己的 Bot。"""

        if type(bot_id) is not int or bot_id <= 0:
            raise BotError("无效的 Bot ID")

        owner_id = owner_id.strip()

        allowed = {
            "name", "persona", "system_prompt",
            "expertise", "style", "api_source", "avatar",
        }
        limits = {
            "name": 50,
            "persona": 200,
            "system_prompt": 4000,
            "expertise": 200,
            "style": 100,
        }

        updates = {}

        for key, value in fields.items():
            if key not in allowed:
                raise BotError(f"不能更新的字段：{key}")

            if key == "api_source":
                updates[key] = self._api_source(value)
            elif key == "avatar":
                updates[key] = self._avatar(value)
            elif value is not None:
                updates[key] = self._text(
                    value, key, max_len=limits[key]
                )
            else:
                updates[key] = None

        if not updates:
            return False

        set_clause = ", ".join(f"{key} = ?" for key in updates)
        params = list(updates.values()) + [bot_id, owner_id]

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                f"""
                UPDATE bots
                SET {set_clause}
                WHERE id = ? AND owner_id = ?
                """,
                params,
            )

        return cursor.rowcount == 1

    def set_enabled(
        self,
        *,
        owner_id: str,
        bot_id: int,
        enabled: bool,
    ) -> bool:
        if type(bot_id) is not int or bot_id <= 0:
            raise BotError("无效的 Bot ID")

        if type(enabled) is not bool:
            raise BotError("enabled 必须是布尔值")

        owner_id = owner_id.strip()

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                UPDATE bots SET enabled = ?
                WHERE id = ? AND owner_id = ?
                """,
                (1 if enabled else 0, bot_id, owner_id),
            )

        return cursor.rowcount == 1

    # ----------------------------------------------------------------
    # 默认 Bot（S6）
    # ----------------------------------------------------------------

    def set_default_bot(
        self,
        *,
        user_id: str,
        bot_id: int,
    ) -> bool:
        """设置用户的默认 Bot。校验 Bot 可见（站长或自己的）。"""

        if not isinstance(user_id, str) or not user_id.strip():
            raise BotError("user_id 不能为空")

        if type(bot_id) is not int or bot_id <= 0:
            raise BotError("无效的 Bot ID")

        user_id = user_id.strip()

        if self.get_visible_bot(
            bot_id=bot_id, owner_id=user_id
        ) is None:
            raise BotError("Bot 不存在或无权访问")

        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO user_defaults (user_id, default_bot_id)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    default_bot_id = excluded.default_bot_id
                """,
                (user_id, bot_id),
            )

        return True

    def get_default_bot(self, user_id: str) -> dict | None:
        """获取用户的默认 Bot；未设置时回退到第一个站长 Bot。"""

        if not isinstance(user_id, str) or not user_id.strip():
            raise BotError("user_id 不能为空")

        user_id = user_id.strip()

        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT default_bot_id FROM user_defaults
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()

            default_id = row["default_bot_id"] if row else None

            if default_id is not None:
                bot_row = conn.execute(
                    """
                    SELECT * FROM bots
                    WHERE id = ? AND enabled = 1
                    """,
                    (default_id,),
                ).fetchone()

                if bot_row is not None:
                    return dict(bot_row)

            platform = conn.execute(
                """
                SELECT * FROM bots
                WHERE owner_id IS NULL AND enabled = 1
                ORDER BY id LIMIT 1
                """,
            ).fetchone()

        return dict(platform) if platform else None
