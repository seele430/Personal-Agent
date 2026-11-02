"""团队任务分配：存储层。

四张表：
- teams       团队（负责人）
- agents      成员（专长、模型、并发上限）
- tasks       任务树（parent_id 表达层级，depends_on 表达依赖）
- task_events 审计流水（状态变化、派发、失败）

设计取舍：
- 任务是**树**（parent_id），不是列表；子任务可以继续分。
- 依赖用 depends_on 存兄弟间下标，落库后换成 task id。
- 与聊天室完全独立，互不影响。
"""

import json

import sqlite3

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


STATUSES = (
    "pending",
    "running",
    "done",
    "failed",
    "skipped",
)


def _now() -> str:

    return datetime.now(timezone.utc).isoformat()


class TeamError(Exception):
    """业务错误（给 API 转 422 用）。"""


class TeamStore:

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            from src import config

            db_path = (
                config.paths.chat_requests_db.parent / "teams.db"
            )

        self.db_path = Path(db_path)

        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.init_db()

    def _connect(self):
        conn = sqlite3.connect(
            self.db_path,
            timeout=10,
        )

        conn.row_factory = sqlite3.Row

        conn.execute("PRAGMA synchronous=NORMAL")

        return conn

    def init_db(self):

        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS teams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    max_workers INTEGER NOT NULL DEFAULT 3,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    team_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    skills TEXT NOT NULL,
                    model TEXT,
                    max_concurrency INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    team_id INTEGER NOT NULL,
                    parent_id INTEGER,
                    title TEXT NOT NULL,
                    skill TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    agent_id INTEGER,
                    depends_on TEXT,
                    input TEXT,
                    output TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_team
                    ON tasks(team_id);

                CREATE INDEX IF NOT EXISTS idx_tasks_parent
                    ON tasks(parent_id);

                CREATE INDEX IF NOT EXISTS idx_events_task
                    ON task_events(task_id);
                """
            )

    # ======================================
    # 团队
    # ======================================

    def create_team(
        self,
        owner_id: str,
        name: str,
        max_workers: int = 3,
    ) -> dict:

        now = _now()

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO teams (owner_id, name, max_workers, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (owner_id, name, max_workers, now),
            )

            team_id = cursor.lastrowid

        return self.get_team(team_id)

    def get_team(self, team_id: int) -> dict | None:

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM teams WHERE id = ?",
                (team_id,),
            ).fetchone()

        return dict(row) if row else None

    def list_teams(self, owner_id: str) -> list[dict]:

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM teams
                WHERE owner_id = ?
                ORDER BY id DESC
                """,
                (owner_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    # ======================================
    # 成员
    # ======================================

    def add_agent(
        self,
        team_id: int,
        name: str,
        skills: list[str],
        model: str | None = None,
        max_concurrency: int = 1,
    ) -> dict:

        cleaned = [
            skill.strip()
            for skill in skills
            if isinstance(skill, str) and skill.strip()
        ]

        if not cleaned:
            raise TeamError("成员至少要有一个专长")

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO agents
                    (team_id, name, skills, model,
                     max_concurrency, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    team_id,
                    name,
                    json.dumps(cleaned, ensure_ascii=False),
                    model,
                    max_concurrency,
                    _now(),
                ),
            )

            agent_id = cursor.lastrowid

        return self.get_agent(agent_id)

    def get_agent(self, agent_id: int) -> dict | None:

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()

        if row is None:
            return None

        item = dict(row)
        item["skills"] = json.loads(item["skills"])

        return item

    def list_agents(self, team_id: int) -> list[dict]:

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM agents
                WHERE team_id = ?
                ORDER BY id ASC
                """,
                (team_id,),
            ).fetchall()

        result = []

        for row in rows:
            item = dict(row)
            item["skills"] = json.loads(item["skills"])
            result.append(item)

        return result

    # ======================================
    # 任务
    # ======================================

    def create_task(
        self,
        *,
        team_id: int,
        title: str,
        skill: str | None = None,
        parent_id: int | None = None,
        depends_on: list[int] | None = None,
        input_text: str | None = None,
        status: str = "pending",
    ) -> dict:

        if status not in STATUSES:
            raise TeamError(f"未知状态：{status}")

        now = _now()

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO tasks (
                    team_id, parent_id, title, skill, status,
                    depends_on, input, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    team_id,
                    parent_id,
                    title,
                    skill,
                    status,
                    json.dumps(depends_on or []),
                    input_text,
                    now,
                    now,
                ),
            )

            task_id = cursor.lastrowid

        self.add_event(task_id, "created", {"status": status})

        return self.get_task(task_id)

    def get_task(self, task_id: int) -> dict | None:

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()

        if row is None:
            return None

        item = dict(row)
        item["depends_on"] = json.loads(item["depends_on"] or "[]")

        return item

    def update_task(self, task_id: int, **fields) -> dict:

        allowed = {
            "status",
            "agent_id",
            "output",
            "error",
            "attempts",
            "depends_on",
        }

        unknown = set(fields) - allowed

        if unknown:
            raise TeamError(f"不允许修改的字段：{unknown}")

        if not fields:
            return self.get_task(task_id)

        if "status" in fields and fields["status"] not in STATUSES:
            raise TeamError(f"未知状态：{fields['status']}")

        payload = dict(fields)

        if "depends_on" in payload:
            payload["depends_on"] = json.dumps(payload["depends_on"])

        payload["updated_at"] = _now()

        assignments = ", ".join(
            f"{key} = ?" for key in payload
        )

        values = list(payload.values()) + [task_id]

        with closing(self._connect()) as conn, conn:
            conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?",
                values,
            )

        return self.get_task(task_id)

    def children(self, parent_id: int) -> list[dict]:

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE parent_id = ?
                ORDER BY id ASC
                """,
                (parent_id,),
            ).fetchall()

        result = []

        for row in rows:
            item = dict(row)
            item["depends_on"] = json.loads(item["depends_on"] or "[]")
            result.append(item)

        return result

    def task_tree(self, root_id: int, depth: int = 0) -> dict | None:
        """递归成树。深度上限防止脏数据成环。"""

        if depth > 12:
            return None

        task = self.get_task(root_id)

        if task is None:
            return None

        task = dict(task)
        task["children"] = [
            child
            for child in (
                self.task_tree(item["id"], depth + 1)
                for item in self.children(root_id)
            )
            if child is not None
        ]

        return task

    # ======================================
    # 审计
    # ======================================

    def add_event(
        self,
        task_id: int,
        kind: str,
        payload: dict | None = None,
    ) -> None:

        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO task_events
                    (task_id, kind, payload, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    task_id,
                    kind,
                    json.dumps(payload or {}, ensure_ascii=False),
                    _now(),
                ),
            )

    def events(self, task_id: int) -> list[dict]:

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_events
                WHERE task_id = ?
                ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()

        return [dict(row) for row in rows]
