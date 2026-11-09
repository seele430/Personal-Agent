"""双 Worker 测试：任务执行、确认门禁、取消与发送认领。"""

import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.qq.inbound import QQInbound
from src.qq.outbox import QQOutbox
from src.qq.worker import ControlWorker, TaskWorker


class FakeClient:
    def __init__(self):
        self.sent = []

    def send_private_msg(self, user_id, text):
        self.sent.append((user_id, text))

        return {"message_id": 1}


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.outbox = QQOutbox(self.root / "outbox.db")
        self.client = FakeClient()
        self.handler = QQInbound(owner_id=7, secret="s", client=self.client, enabled=True)

    def enqueue(self, text, queue="task", message_id="1"):
        self.outbox.enqueue(message_id=message_id, sender=7, raw_text=text, queue=queue)

        return message_id

    def pending(self, message_id="1", intent="status", params=None, state="pending_task"):
        self.outbox.set_routing(
            message_id,
            intent=intent,
            params=params or {},
            params_hash="h",
            decision_source="rule",
            state=state,
        )

    def task_worker(self):
        return TaskWorker(outbox=self.outbox, client=self.client, handler=self.handler)

    def control_worker(self):
        return ControlWorker(outbox=self.outbox, client=self.client, handler=self.handler)


class TaskWorkerCase(WorkerCase):
    def test_status(self):
        self.enqueue("/status")
        self.pending()

        self.task_worker().run_once()

        self.assertEqual(self.client.sent[0][1], "Personal Agent 在线；QQ 入站已接通。")
        self.assertEqual(self.outbox.get("1")["state"], "sent")

    def test_danger_denied(self):
        self.enqueue("执行 rm -rf /")
        self.pending(intent="exec")

        self.task_worker().run_once()

        self.assertIn("不支持执行命令", self.client.sent[0][1])
        self.assertEqual(self.outbox.get("1")["intent"], "exec")

    def test_unavailable(self):
        self.enqueue("画一只猫")
        self.pending()

        self.task_worker().run_once()

        self.assertIn("暂未开放出图", self.client.sent[0][1])

    def test_clarify_missing_path(self):
        self.enqueue("读一下这个")
        self.pending()

        self.task_worker().run_once()

        self.assertIn("再说清楚", self.client.sent[0][1])

    def test_read_file_requires_confirm(self):
        workspace = self.root / "ws"
        workspace.mkdir()
        (workspace / "a.md").write_text("hello", encoding="utf-8")

        self.enqueue("读一下 a.md")
        self.pending()

        with patch.dict("os.environ", {"PA_QQ_WORKSPACE": str(workspace)}, clear=False):
            self.task_worker().run_once()

        row = self.outbox.get("1")
        self.assertEqual(row["state"], "awaiting_confirm")
        self.assertEqual(len(row["confirm_id"]), 8)
        self.assertIn("/confirm", self.client.sent[0][1])

    def test_plain_text_ask(self):
        self.enqueue("1+1 等于几")
        self.pending()

        with patch.object(self.handler, "reply_for", return_value="2"):
            self.task_worker().run_once()

        self.assertEqual(self.client.sent[0][1], "2")

    def test_cancel_during_run_prevents_send(self):
        self.enqueue("1+1 等于几")
        self.pending()

        def cancel_then_answer(*args, **kwargs):
            self.outbox.cancel(sender=7, target="1")

            return "2"

        with patch.object(self.handler, "reply_for", side_effect=cancel_then_answer):
            self.task_worker().run_once()

        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.outbox.get("1")["state"], "cancelled")


class ControlWorkerCase(WorkerCase):
    def test_confirm_command_consumes(self):
        self.enqueue("读一下 a.md", message_id="task-1")
        self.pending("task-1", intent="read_file")
        self.outbox.set_awaiting_confirm("task-1", confirm_id="K7M2P9QX", expires_at=9e9)

        self.enqueue("/confirm K7M2P9QX", queue="control", message_id="cmd-1")
        self.pending("cmd-1", intent="confirm", params={"confirmation_id": "K7M2P9QX"}, state="pending_control")

        self.control_worker().run_once()

        self.assertIn("已确认", self.client.sent[0][1])
        self.assertEqual(self.outbox.get("task-1")["state"], "pending_task")

    def test_confirm_invalid(self):
        self.enqueue("/confirm WRONG234", queue="control", message_id="cmd-1")
        self.pending("cmd-1", intent="confirm", params={"confirmation_id": "WRONG234"}, state="pending_control")

        self.control_worker().run_once()

        self.assertIn("失效", self.client.sent[0][1])

    def test_cancel_pending(self):
        self.enqueue("1+1 等于几", message_id="task-1")
        self.pending("task-1", intent="ask")

        self.enqueue("/cancel task-1", queue="control", message_id="cmd-1")
        self.pending("cmd-1", intent="cancel", params={"target": "task-1"}, state="pending_control")

        self.control_worker().run_once()

        self.assertIn("已取消", self.client.sent[0][1])
        self.assertEqual(self.outbox.get("task-1")["state"], "cancelled")

    def test_cancel_running_message(self):
        self.enqueue("1+1 等于几", message_id="task-1")
        self.pending("task-1", intent="ask")
        self.outbox.claim("task")

        self.enqueue("/cancel task-1", queue="control", message_id="cmd-1")
        self.pending("cmd-1", intent="cancel", params={"target": "task-1"}, state="pending_control")

        self.control_worker().run_once()

        self.assertIn("仍可能产生费用", self.client.sent[0][1])

    def test_cancel_missing(self):
        self.enqueue("/cancel 999", queue="control", message_id="cmd-1")
        self.pending("cmd-1", intent="cancel", params={"target": "999"}, state="pending_control")

        self.control_worker().run_once()

        self.assertIn("找不到", self.client.sent[0][1])




class ConcurrencyCase(WorkerCase):
    def test_confirm_processed_while_task_running(self):
        self.enqueue("1+1 等于几", message_id="task-1")
        self.pending("task-1", intent="ask")
        self.outbox.claim("task")  # 模拟长任务正在执行

        self.enqueue("读一下 a.md", message_id="task-2")
        self.pending("task-2", intent="read_file")
        self.outbox.set_awaiting_confirm("task-2", confirm_id="K7M2P9QX", expires_at=9e9)

        self.enqueue("/confirm K7M2P9QX", queue="control", message_id="cmd-1")
        self.pending("cmd-1", intent="confirm", params={"confirmation_id": "K7M2P9QX"}, state="pending_control")

        self.control_worker().run_once()

        self.assertIn("已确认", self.client.sent[-1][1])
        self.assertEqual(self.outbox.get("task-2")["state"], "pending_task")

    def test_cancel_after_sending_reports_sending(self):
        self.enqueue("/status", message_id="task-1")
        self.pending("task-1", intent="status")
        self.outbox.claim("task")
        self.outbox.claim_send("task-1")

        self.enqueue("/cancel task-1", queue="control", message_id="cmd-1")
        self.pending("cmd-1", intent="cancel", params={"target": "task-1"}, state="pending_control")

        self.control_worker().run_once()

        self.assertIn("无法保证取消", self.client.sent[-1][1])

    def test_notice_keeps_awaiting_state(self):
        workspace = self.root / "ws2"
        workspace.mkdir()
        (workspace / "b.md").write_text("x", encoding="utf-8")

        self.enqueue("读一下 b.md", message_id="task-1")
        self.pending("task-1", intent="read_file")

        with patch.dict("os.environ", {"PA_QQ_WORKSPACE": str(workspace)}, clear=False):
            self.task_worker().run_once()

        self.assertEqual(self.outbox.get("task-1")["state"], "awaiting_confirm")
        self.assertIn("/confirm", self.client.sent[-1][1])



    def test_confirmed_read_executes_without_reasking(self):
        workspace = self.root / "ws3"
        workspace.mkdir()
        (workspace / "c.md").write_text("hello world", encoding="utf-8")

        self.enqueue("读一下 c.md", message_id="task-1")
        self.pending("task-1", intent="read_file", params={"path": "c.md"})
        self.outbox.set_awaiting_confirm("task-1", confirm_id="K7M2P9QX", expires_at=9e9)
        self.outbox.consume_confirm(sender=7, confirm_id="K7M2P9QX")

        with patch.dict("os.environ", {"PA_QQ_WORKSPACE": str(workspace)}, clear=False):
            self.task_worker().run_once()

        self.assertIn("hello world", self.client.sent[-1][1])
        self.assertNotIn("/confirm", self.client.sent[-1][1])
        self.assertEqual(self.outbox.get("task-1")["state"], "sent")




class ContractCase(WorkerCase):
    def test_disabled_intent_is_rejected(self):
        import dataclasses
        from src.qq import intents as intents_module
        from src.qq import worker as worker_module

        self.enqueue("读一下 a.md", message_id="task-1")
        self.pending("task-1", intent="read_file", params={"path": "a.md"})

        disabled = dataclasses.replace(intents_module.get_spec("read_file"), enabled=False)

        with patch(
            "src.qq.worker.check_contract",
            return_value=(disabled, {"path": "a.md"}, "disabled"),
        ):
            self.task_worker().run_once()

        self.assertIn("未启用", self.client.sent[-1][1])
        self.assertEqual(self.outbox.get("task-1")["state"], "denied")

    def test_invalid_params_rejected(self):
        self.enqueue("读一下 a.md", message_id="task-1")
        self.pending("task-1", intent="read_file", params={"path": "a.md"})

        bad = {
            "intent": "read_file",
            "params": {"path": "a.md", "extra": 1},
            "action": "confirm",
            "reason": "read_file",
            "source": "rule",
        }

        with patch("src.qq.worker.router.classify", return_value=bad):
            self.task_worker().run_once()

        self.assertIn("参数不合法", self.client.sent[-1][1])
        self.assertEqual(self.outbox.get("task-1")["state"], "denied")

    def test_unavailable_has_terminal_state(self):
        self.enqueue("画一只猫")
        self.pending()

        self.task_worker().run_once()

        self.assertEqual(self.outbox.get("1")["state"], "unavailable")

    def test_clarify_has_terminal_state(self):
        self.enqueue("读一下这个")
        self.pending()

        self.task_worker().run_once()

        self.assertEqual(self.outbox.get("1")["state"], "clarified")

    def test_control_terminal_state_is_done(self):
        self.enqueue("/cancel 999", queue="control", message_id="cmd-1")
        self.pending("cmd-1", intent="cancel", params={"target": "999"}, state="pending_control")

        self.control_worker().run_once()

        self.assertEqual(self.outbox.get("cmd-1")["state"], "done")



    def test_confirmed_read_rechecks_enabled(self):
        import dataclasses
        from src.qq import intents as intents_module
        from src.qq import worker as worker_module

        workspace = self.root / "ws4"
        workspace.mkdir()
        (workspace / "d.md").write_text("secret", encoding="utf-8")

        self.enqueue("读一下 d.md", message_id="task-1")
        self.pending("task-1", intent="read_file", params={"path": "d.md"})
        self.outbox.set_awaiting_confirm("task-1", confirm_id="K7M2P9QX", expires_at=9e9)
        self.outbox.consume_confirm(sender=7, confirm_id="K7M2P9QX")

        disabled = dataclasses.replace(worker_module.check_contract("read_file", {"path": "d.md"})[0], enabled=False)

        with patch.dict("os.environ", {"PA_QQ_WORKSPACE": str(workspace)}, clear=False), \
             patch(
                 "src.qq.worker.check_contract",
                 return_value=(disabled, {"path": "d.md"}, "disabled"),
             ):
            self.task_worker().run_once()

        self.assertEqual(self.outbox.get("task-1")["state"], "denied")
        self.assertNotIn("secret", self.client.sent[-1][1])

    def test_confirm_disabled_blocks_control(self):
        import dataclasses
        from src.qq import intents as intents_module
        from src.qq import worker as worker_module

        self.enqueue("/confirm K7M2P9QX", queue="control", message_id="cmd-1")
        self.pending("cmd-1", intent="confirm", params={"confirmation_id": "K7M2P9QX"}, state="pending_control")

        disabled = dataclasses.replace(worker_module.check_contract("confirm", {"confirmation_id": "K7M2P9QX"})[0], enabled=False)

        with patch(
            "src.qq.worker.check_contract",
            return_value=(disabled, {"confirmation_id": "K7M2P9QX"}, "disabled"),
        ):
            self.control_worker().run_once()

        self.assertIn("未启用", self.client.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
