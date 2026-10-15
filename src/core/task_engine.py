import sqlite3
from contextlib import closing
from pathlib import Path
from src.config import paths

BASE_DIR = Path(__file__).resolve().parents[2]


class NotificationNotFound(Exception):
    """站内通知不存在，或不属于当前用户。"""


MAX_IN_APP_NOTIFICATIONS = 50


# 连接写法说明：
#   with closing(sqlite3.connect(...)) as conn, conn:
# 内层 conn 负责事务（成功提交、异常回滚），
# 外层 closing 在退出时关闭连接，释放 Windows 文件句柄。
# 单独使用 `with sqlite3.connect(...) as conn` 只会提交，不会关闭连接。


def init_task_db():
    """初始化任务状态数据库。"""
    with closing(sqlite3.connect(paths.tasks_db, timeout=10)) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS monitor_state (
                monitor_id TEXT PRIMARY KEY,
                initialized_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_items (
                monitor_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                title TEXT,
                link TEXT,
                seen_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (monitor_id, item_id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                sent_at TEXT,
                UNIQUE (monitor_id, item_id)
            )
        """)

        # 通知必须能归属到人。
        # 与 monitor_tasks.archived_at 一样，用非破坏性迁移补列：
        # 旧记录保持 NULL —— 既有的"不把历史数据自动分配给站长"政策。
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(notification_outbox)"
            )
        }

        if "owner_id" not in columns:
            conn.execute(
                "ALTER TABLE notification_outbox "
                "ADD COLUMN owner_id TEXT"
            )


def list_in_app_notifications(
    user_id: str,
    *,
    limit: int = MAX_IN_APP_NOTIFICATIONS,
) -> list[dict]:
    """读取某个用户尚未确认的站内通知。

    只按服务端认证后的 user_id 过滤，
    不接受调用方传入的其它身份。
    """

    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id 不能为空")

    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("limit 无效")

    init_task_db()

    with closing(
        sqlite3.connect(paths.tasks_db, timeout=10)
    ) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT id, monitor_id, item_id, message,
                   created_at
            FROM notification_outbox
            WHERE owner_id = ?
              AND status = 'in_app'
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id.strip(), limit),
        ).fetchall()

    return [dict(row) for row in rows]


def mark_notification_read(
    *,
    user_id: str,
    notification_id: int,
) -> dict:
    """把一条站内通知标记为已读（只能操作自己的）。"""

    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id 不能为空")

    if (
        type(notification_id) is not int
        or notification_id <= 0
    ):
        raise ValueError("通知 ID 无效")

    init_task_db()

    with closing(
        sqlite3.connect(paths.tasks_db, timeout=10)
    ) as conn:

        try:
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute(
                """
                SELECT id, status
                FROM notification_outbox
                WHERE id = ? AND owner_id = ?
                """,
                (notification_id, user_id.strip()),
            ).fetchone()

            if row is None:
                raise NotificationNotFound(
                    "通知不存在或无权访问"
                )

            if row[1] != "in_app":
                conn.commit()

                return {
                    "id": notification_id,
                    "status": row[1],
                    "changed": False,
                }

            conn.execute(
                """
                UPDATE notification_outbox
                SET status = 'read'
                WHERE id = ?
                  AND owner_id = ?
                  AND status = 'in_app'
                """,
                (notification_id, user_id.strip()),
            )

            conn.commit()

            return {
                "id": notification_id,
                "status": "read",
                "changed": True,
            }

        except Exception:
            conn.rollback()
            raise


def detect_new_items(
    monitor_id: str,
    articles: list[dict],
    *,
    label: str = "更新",
) -> list[dict]:
    """
    首次检查建立基线。
    后续发现新内容时，将通知保存到 Outbox。

    `label` 是通知文案的前缀，由数据源自己声明（RSS 用
    "RSS 更新"、网页内容变化用"页面更新"）—— 写死成 "RSS 更新"
    会让非 RSS 源的通知自相矛盾。
    """

    if not monitor_id.strip():
        raise ValueError("monitor_id 不能为空")

    if not articles:
        raise ValueError("文章列表不能为空")

    init_task_db()

    new_articles = []

    with closing(sqlite3.connect(paths.tasks_db, timeout=10)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")

        # 通知要归属到监控任务的主人。
        # 任务的 owner_id 就在同一个库的 monitor_tasks 上。
        # 独立使用任务引擎（表不存在）或任务查不到时保持 NULL：
        # 宁可标记为"未归属"，也不猜一个归属。
        owner_id = None

        has_monitor_tasks = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table'
              AND name = 'monitor_tasks'
            """
        ).fetchone() is not None

        if has_monitor_tasks:
            owner_row = conn.execute(
                """
                SELECT owner_id
                FROM monitor_tasks
                WHERE id = ?
                """,
                (monitor_id,)
            ).fetchone()

            if owner_row is not None:
                owner_id = owner_row[0]

        initialized = conn.execute(
            """
            SELECT 1 FROM monitor_state
            WHERE monitor_id = ?
            """,
            (monitor_id,)
        ).fetchone() is not None

        for article in articles:
            item_id = article.get("id")

            if not isinstance(item_id, str) or not item_id:
                continue

            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO seen_items
                (monitor_id, item_id, title, link)
                VALUES (?, ?, ?, ?)
                """,
                (
                    monitor_id,
                    item_id,
                    article.get("title", ""),
                    article.get("link", "")
                )
            )

            if initialized and cursor.rowcount == 1:
                title = article.get("title") or "新文章"
                link = article.get("link") or ""

                message = f"{label}：{title}\n{link}"

                conn.execute(
                    """
                    INSERT OR IGNORE INTO notification_outbox
                    (monitor_id, item_id, message, status, owner_id)
                    VALUES (?, ?, ?, 'pending', ?)
                    """,
                    (monitor_id, item_id, message, owner_id)
                )

                new_articles.append(article)

        if not initialized:
            conn.execute(
                """
                INSERT INTO monitor_state (monitor_id)
                VALUES (?)
                """,
                (monitor_id,)
            )

    return new_articles