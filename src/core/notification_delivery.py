"""通知投递策略：这条通知该走哪个渠道。

为什么需要它：外发 worker 以前把**所有**通知都发到同一个全局
`NTFY_TOPIC`（站长的手机）。于是管理员建的监控任务一旦有新内容，
结果会推到**站长**手机上 —— 既是串号，也是信息泄露。

现在的口径（最保守的默认）：

| 情况 | 渠道 | 理由 |
| --- | --- | --- |
| 任务主人是站长（role == owner） | `ntfy` | 沿用既有全局 topic |
| 任务主人是其他角色 | `in_app` | 站内可见，由本人读取并确认 |
| 主人已不存在 / 查不到角色 | `in_app` | **宁可在站内没人看，也不误推别人手机** |
| 历史行没有 owner_id | `ntfy` | 迁移前的行本就发往全局 topic，保持原行为 |

渠道在**投递时**判定（不是入队时快照），所以角色变更会立刻生效。
"""

import sqlite3

from contextlib import closing
from pathlib import Path

from src.config import paths


NTFY = "ntfy"
IN_APP = "in_app"

# worker 只搬运这一种；另一种由用户通过接口自取。
CHANNELS = frozenset({NTFY, IN_APP})

OWNER_ROLE = "owner"


def channel_for_role(role: str | None) -> str:
    return NTFY if role == OWNER_ROLE else IN_APP


def role_of_user(user_id: str | None) -> str | None:
    """查 auth_users 拿角色；查不到就返回 None（不猜）。"""

    if not isinstance(user_id, str) or not user_id.strip():
        return None

    path = Path(paths.auth_users_db)

    if not path.is_file():
        return None

    try:
        with closing(
            sqlite3.connect(path, timeout=10)
        ) as conn:
            row = conn.execute(
                """
                SELECT role
                FROM auth_users
                WHERE id = ?
                """,
                (user_id.strip(),),
            ).fetchone()

    except sqlite3.Error:
        # 用户库不可读时按"查不到"处理，落到最保守的渠道。
        return None

    if row is None:
        return None

    return row[0]


def channel_for_owner(owner_id: str | None) -> str:
    """按任务主人决定渠道。"""

    # 迁移前的历史行没有归属，只能沿用旧的全局推送。
    if owner_id is None:
        return NTFY

    return channel_for_role(role_of_user(owner_id))
