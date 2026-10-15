"""统一收件箱：把"待确认的站内消息"聚合成一个视图。

站内现在有两套来源：

| 来源 | 表 | 什么时候算"待确认" |
| --- | --- | --- |
| 监控任务的新内容 | `notification_outbox` | `status = 'in_app'` |
| 到点的站内提醒 | `reminder_requests` | `delivery = 'in_app'` 且已到期且 `pending` |

它们的表结构与状态机都不同，但用户面对的是同一件事：**有什么在等我确认**。

这里只做**读取聚合 + 确认路由**，不做数据迁移 —— 合并成一张表要改动
已经稳定的提醒调度与提醒面板，收益不抵风险。等两边都稳定运行一段时间，
再考虑真正的合并。

注意：站长的提醒走 ntfy（不进站内），所以不会出现在收件箱里 ——
这与"非站长走站内"的投递口径是一致的。
"""

from src.core import reminder_identity
from src.core.reminder_store import (
    ReminderNotFound,
    ReminderStore,
)
from src.core.task_engine import (
    MAX_IN_APP_NOTIFICATIONS,
    NotificationNotFound,
    list_in_app_notifications,
    mark_notification_read,
)


MONITOR = "monitor"
REMINDER = "reminder"

KINDS = (MONITOR, REMINDER)


class InboxNotFound(Exception):
    """收件箱条目不存在，或不属于当前用户。

    两种来源的"不存在"在这里统一成一个异常，
    这样接口层可以给出同一个响应，不泄露"这条存在但不属于你"。
    """


def _monitor_items(user_id, limit):
    rows = list_in_app_notifications(
        user_id, limit=limit
    )

    return [
        {
            "kind": MONITOR,
            "id": row["id"],
            "message": row["message"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _reminder_items(user_id):
    # 必须走模块属性读取：测试的 isolate_paths 会替换
    # reminder_identity.REMINDER_DB_PATH 上的那个名字，
    # 早绑定（from ... import REMINDER_DB_PATH）会拿不到替换后的值。
    store = ReminderStore(
        reminder_identity.REMINDER_DB_PATH
    )

    # 全新部署里可能还没有提醒表（没人建过提醒），
    # 读之前先安全补一次，否则收件箱会直接 500。
    # 与 reminder_api 的做法一致：只建缺失的表，不改动已有数据。
    store.init_db()

    rows = store.due_in_app(user_id=user_id)

    return [
        {
            "kind": REMINDER,
            "id": row["id"],
            "message": row["message"],
            "created_at": row["remind_at"],
        }
        for row in rows
    ]


def list_inbox(
    user_id: str,
    *,
    limit: int = MAX_IN_APP_NOTIFICATIONS,
) -> list[dict]:
    """聚合当前用户所有待确认的站内消息，新的在前。"""

    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id 不能为空")

    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("limit 无效")

    items = _monitor_items(user_id, limit)
    items.extend(_reminder_items(user_id))

    # created_at 一定存在（两张表都是 NOT NULL），
    # 但排序键仍做一次兜底，避免脏数据让整个收件箱报错。
    items.sort(
        key=lambda item: item["created_at"] or "",
        reverse=True,
    )

    return items[:limit]


def acknowledge(
    *,
    user_id: str,
    kind: str,
    item_id: int,
) -> dict:
    """确认一条收件箱条目，路由到对应来源。"""

    if kind not in KINDS:
        raise ValueError(
            f"未知的收件箱类型：{kind!r}"
        )

    if kind == MONITOR:
        try:
            return mark_notification_read(
                user_id=user_id,
                notification_id=item_id,
            )

        except NotificationNotFound:
            raise InboxNotFound(
                "收件箱条目不存在或无权访问"
            ) from None

    try:
        store = ReminderStore(
            reminder_identity.REMINDER_DB_PATH
        )

        return store.mark_delivered(
            user_id=user_id,
            reminder_id=item_id,
        )

    except ReminderNotFound:
        raise InboxNotFound(
            "收件箱条目不存在或无权访问"
        ) from None
