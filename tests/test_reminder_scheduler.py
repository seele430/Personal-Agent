"""提醒投递：调度器从权威表取件，旧表只做镜像与兼容。

锁定三条性质：

1. 只投递 `delivery = 'ntfy'` 的到期提醒 —— 成员的站内提醒**绝不**
   推到全站 ntfy（这是这套设计里最容易发生的泄露）；
2. 投递成功后结单，重复扫描不会重复推送（幂等）；
3. 旧表镜像与新表同进同退；没有任何新表记录指向的历史待发行
   仍然能送出（不吞用户已经设好的提醒），且两条路径不重复推送。
"""

import sqlite3
import unittest

from contextlib import closing
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.helper import isolate_paths
from src import scheduler
from src.config import paths
from src.core.reminder_store import ReminderStore


class TestReminderDelivery(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        isolate_paths(self, self.temp)

        self.store = ReminderStore(paths.reminders_db)
        self.store.init_db()

    # ---------- 装置 ----------

    def create(
        self,
        *,
        user_id,
        role,
        message,
        offset_minutes=-10,
        minutes=1,
        tag="a",
    ):
        """offset_minutes<0 表示这条提醒已经到期。"""

        base = datetime.now(timezone.utc) + timedelta(
            minutes=offset_minutes
        )

        return self.store.create(
            user_id=user_id,
            request_id="req_%s_%s_%s" % (
                user_id,
                tag,
                minutes,
            ),
            role=role,
            message=message,
            minutes=minutes,
            now=base,
        )

    def states(self):
        with closing(
            sqlite3.connect(paths.reminders_db)
        ) as conn:
            conn.row_factory = sqlite3.Row

            return {
                row["id"]: dict(row)
                for row in conn.execute(
                    """
                    SELECT id, status, delivery,
                           legacy_reminder_id
                    FROM reminder_requests
                    """
                )
            }

    def legacy_status(self, legacy_id):
        with closing(
            sqlite3.connect(paths.reminders_db)
        ) as conn:
            return conn.execute(
                "SELECT status FROM reminders WHERE id = ?",
                (legacy_id,),
            ).fetchone()[0]

    def add_legacy_only(self, message, minutes_ago=5):
        remind_at = (
            datetime.now(timezone.utc)
            - timedelta(minutes=minutes_ago)
        ).isoformat()

        with closing(
            sqlite3.connect(paths.reminders_db)
        ) as conn, conn:
            conn.execute(
                """
                INSERT INTO reminders
                (message, remind_at, status)
                VALUES (?, ?, 'pending')
                """,
                (message, remind_at),
            )

    # ---------- 取件口 ----------

    def test_due_ntfy_filters_by_delivery_and_time(self):
        owner = self.create(
            user_id="owner_001",
            role="owner",
            message="站长的",
        )
        self.create(
            user_id="member_001",
            role="member",
            message="成员的",
            tag="member",
        )
        self.create(
            user_id="owner_001",
            role="owner",
            message="将来的",
            offset_minutes=0,
            minutes=60,
            tag="future",
        )

        due = self.store.due_ntfy()

        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["id"], owner["id"])
        self.assertEqual(due[0]["message"], "站长的")

    # ---------- 投递 ----------

    def test_only_due_ntfy_is_pushed(self):
        owner = self.create(
            user_id="owner_001",
            role="owner",
            message="站长的",
        )
        member = self.create(
            user_id="member_001",
            role="member",
            message="成员的",
            tag="member",
        )
        future = self.create(
            user_id="owner_001",
            role="owner",
            message="将来的",
            offset_minutes=0,
            minutes=60,
            tag="future",
        )

        with patch.object(
            scheduler, "send_notification"
        ) as sender:
            scheduler.check_reminders()

        # 只有站长那条被推送
        self.assertEqual(sender.call_count, 1)
        self.assertIn("站长的", sender.call_args[0][0])
        self.assertIn("⏰", sender.call_args[0][0])

        rows = self.states()

        self.assertEqual(rows[owner["id"]]["status"], "sent")

        # 站内提醒由前端确认送达，调度器不碰
        self.assertEqual(
            rows[member["id"]]["status"], "pending"
        )
        self.assertEqual(
            rows[future["id"]]["status"], "pending"
        )

    def test_repeat_scan_does_not_resend(self):
        self.create(
            user_id="owner_001",
            role="owner",
            message="站长的",
        )

        with patch.object(
            scheduler, "send_notification"
        ) as sender:
            scheduler.check_reminders()
            scheduler.check_reminders()
            scheduler.check_reminders()

        self.assertEqual(sender.call_count, 1)

    def test_legacy_mirror_is_synced_on_delivery(self):
        owner = self.create(
            user_id="owner_001",
            role="owner",
            message="站长的",
        )

        legacy_id = owner["legacy_reminder_id"]

        self.assertIsNotNone(legacy_id)
        self.assertEqual(
            self.legacy_status(legacy_id), "pending"
        )

        with patch.object(
            scheduler, "send_notification"
        ):
            scheduler.check_reminders()

        # 镜像与新表同进同退：否则旧表看起来还在待发
        self.assertEqual(
            self.legacy_status(legacy_id), "sent"
        )

    def test_legacy_only_row_is_still_delivered(self):
        """按人投递上线前留下的待发行也要送出，不能被吞掉。"""

        self.add_legacy_only("历史的")

        with patch.object(
            scheduler, "send_notification"
        ) as sender:
            scheduler.check_reminders()

        self.assertEqual(sender.call_count, 1)
        self.assertIn("历史的", sender.call_args[0][0])

        # 结单之后不再重复推送
        with patch.object(
            scheduler, "send_notification"
        ) as again:
            scheduler.check_reminders()

        self.assertEqual(again.call_count, 0)

    def test_new_and_legacy_paths_do_not_overlap(self):
        """同一条提醒不会既从新表又从旧表被推送。"""

        self.create(
            user_id="owner_001",
            role="owner",
            message="新表的",
        )
        self.add_legacy_only("历史的")

        with patch.object(
            scheduler, "send_notification"
        ) as sender:
            scheduler.check_reminders()

        # 一条新表 + 一条历史 = 恰好两次，且内容不同
        self.assertEqual(sender.call_count, 2)

        messages = [
            call[0][0] for call in sender.call_args_list
        ]

        self.assertEqual(len(set(messages)), 2)

    def test_member_reminder_never_reaches_ntfy(self):
        """成员的站内提醒在任何情况下都不进全站 ntfy。"""

        self.create(
            user_id="member_001",
            role="member",
            message="成员的私事",
        )

        with patch.object(
            scheduler, "send_notification"
        ) as sender:
            scheduler.check_reminders()

        sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
