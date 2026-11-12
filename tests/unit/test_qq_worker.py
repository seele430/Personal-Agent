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

    def test_long_answer_splits_into_messages(self):
        self.enqueue("1+1 等于几")
        self.pending()
        answer = "\n\n".join("段落内容。" * 50 for _ in range(4))

        with patch.object(self.handler, "reply_for", return_value=answer):
            self.task_worker().run_once()

        self.assertGreaterEqual(len(self.client.sent), 2)
        row = self.outbox.get("1")
        self.assertEqual(row["state"], "sent")
        self.assertGreater(len(row["reply"]), 800)

    def test_split_can_be_disabled(self):
        self.enqueue("1+1 等于几")
        self.pending()

        with patch.dict("os.environ", {"PA_QQ_SPLIT_REPLY": "0"}, clear=False):
            with patch.object(self.handler, "reply_for", return_value="长" * 900):
                self.task_worker().run_once()

        self.assertEqual(len(self.client.sent), 1)
        self.assertLessEqual(len(self.client.sent[0][1]), 820)
        self.assertIn("已省略", self.client.sent[0][1])

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



    def test_continue_uses_latest_turn(self):
        from src.qq.threads import ThreadStore

        threads = ThreadStore(self.root / "threads.db")
        threads.append_turn(sender=7, question="3+3", answer="6", message_id="old")
        captured = {}

        def capture(command, question):
            captured["prompt"] = question

            return "答案"

        self.enqueue("继续，说明怎么算的")
        self.pending("1", intent="ask", params={"question": "继续，说明怎么算的"})

        with patch.object(self.handler, "reply_for", side_effect=capture):
            TaskWorker(
                outbox=self.outbox, client=self.client, handler=self.handler, threads=threads
            ).run_once()

        self.assertIn("3+3", captured["prompt"])
        self.assertIn("继续", captured["prompt"])
        self.assertEqual(self.outbox.get("1")["state"], "sent")

    def test_continue_without_history_skips_provider(self):
        from src.qq.threads import ThreadStore

        threads = ThreadStore(self.root / "threads.db")
        self.enqueue("继续")
        self.pending("1", intent="ask", params={"question": "继续"})
        calls = []

        with patch.object(self.handler, "reply_for", side_effect=lambda *a: calls.append(a) or "x"):
            TaskWorker(
                outbox=self.outbox, client=self.client, handler=self.handler, threads=threads
            ).run_once()

        self.assertEqual(calls, [])
        self.assertIn("没有可继续", self.client.sent[0][1])

    def test_reset_clears_sender_context(self):
        from src.qq.threads import ThreadStore

        threads = ThreadStore(self.root / "threads.db")
        threads.append_turn(sender=7, question="q", answer="a", message_id="old")
        self.enqueue("/reset")
        self.pending("1", intent="reset")

        TaskWorker(
            outbox=self.outbox, client=self.client, handler=self.handler, threads=threads
        ).run_once()

        self.assertEqual(threads.count(7), 0)
        self.assertIn("已清除", self.client.sent[0][1])

    def test_send_failure_does_not_save_context(self):
        from src.qq.threads import ThreadStore

        threads = ThreadStore(self.root / "threads.db")

        from src.qq.onebot import OneBotError

        class BrokenClient(FakeClient):
            def send_private_msg(self, user_id, text):
                raise OneBotError("send failed")

        self.enqueue("1+1")
        self.pending("1", intent="ask", params={"question": "1+1"})
        worker = TaskWorker(
            outbox=self.outbox, client=BrokenClient(), handler=self.handler, threads=threads
        )
        worker.run_once()

        self.assertEqual(threads.count(7), 0)



    def test_context_store_failure_is_audited_and_worker_survives(self):
        from src.qq.threads import ThreadStore
        from unittest.mock import patch as _patch

        threads = ThreadStore(self.root / "threads.db")
        self.enqueue("1+1", message_id="1")
        self.pending("1", intent="ask", params={"question": "1+1"})

        broken = _patch.object(threads, "append_turn", side_effect=RuntimeError("db locked"))

        with broken, _patch.object(self.handler, "reply_for", return_value="2"):
            TaskWorker(
                outbox=self.outbox, client=self.client, handler=self.handler, threads=threads
            ).run_once()

        self.assertEqual(self.outbox.get("1")["state"], "sent")
        actions = [event for event in self.handler.audit]
        self.assertTrue(any(event.get("action") == "context_store_failed" for event in actions))

        self.enqueue("2+2", message_id="2")
        self.pending("2", intent="ask", params={"question": "2+2"})

        with _patch.object(self.handler, "reply_for", return_value="4"):
            TaskWorker(
                outbox=self.outbox, client=self.client, handler=self.handler, threads=threads
            ).run_once()

        self.assertEqual(self.outbox.get("2")["state"], "sent")

    def test_long_answer_saved_equals_sent(self):
        from src.qq.threads import ThreadStore
        from src.qq.worker import MAX_REPLY_CHARS

        threads = ThreadStore(self.root / "threads.db")
        long_answer = "长" * 900
        self.enqueue("请写长文", message_id="1")
        self.pending("1", intent="ask", params={"question": "请写长文"})

        with patch.object(self.handler, "reply_for", return_value=long_answer):
            TaskWorker(
                outbox=self.outbox, client=self.client, handler=self.handler, threads=threads
            ).run_once()

        sent_texts = [text for _, text in self.client.sent]
        joined = "\n".join(sent_texts)
        turn = threads.latest_turn(7)

        self.assertGreaterEqual(len(sent_texts), 2)
        self.assertTrue(all(len(text) <= MAX_REPLY_CHARS for text in sent_texts))
        self.assertEqual(turn["answer"], joined)
        self.assertEqual(self.outbox.get("1")["reply"], joined)



    def test_plain_ask_does_not_use_context(self):
        from src.qq.threads import ThreadStore

        threads = ThreadStore(self.root / "threads.db")
        threads.append_turn(sender=7, question="3+3", answer="6", message_id="old")
        captured = {}

        def capture(command, question):
            captured["prompt"] = question

            return "4"

        self.enqueue("/ask 2+2")
        self.pending("1", intent="ask", params={"question": "2+2"})

        with patch.object(self.handler, "reply_for", side_effect=capture):
            TaskWorker(
                outbox=self.outbox, client=self.client, handler=self.handler, threads=threads
            ).run_once()

        self.assertEqual(captured["prompt"], "2+2")

    def test_half_turn_is_ignored(self):
        from src.qq.threads import ThreadStore

        threads = ThreadStore(self.root / "threads.db")

        with threads._session() as connection:
            connection.execute(
                "INSERT INTO qq_threads (sender, seq, role, content, source_message_id, created_at) "
                "VALUES (7, 1, 'user', 'q', 'm', 1.0)"
            )

        self.enqueue("继续")
        self.pending("1", intent="ask", params={"question": "继续"})
        calls = []

        with patch.object(self.handler, "reply_for", side_effect=lambda *a: calls.append(a) or "x"):
            TaskWorker(
                outbox=self.outbox, client=self.client, handler=self.handler, threads=threads
            ).run_once()

        self.assertEqual(calls, [])
        self.assertIn("没有可继续", self.client.sent[0][1])

    def test_context_budget_and_current_question(self):
        from src.qq.threads import ThreadStore
        from src.qq.worker import build_context_prompt

        threads = ThreadStore(self.root / "threads.db")
        threads.append_turn(sender=7, question="问" * 600, answer="答" * 1500, message_id="old")
        previous = threads.latest_turn(7)
        prompt, chars = build_context_prompt("当前问题完整保留", previous)

        self.assertLessEqual(len(prompt), 2000)
        self.assertIn("当前问题完整保留", prompt)
        self.assertLess(chars, 2000)

    def test_reset_contract_combination(self):
        from src.qq.inbound import QQInbound
        from src.qq.intents import get_spec
        from src.qq.router import classify

        spec = get_spec("reset")
        self.assertIsNotNone(spec)
        self.assertTrue(spec.enabled)
        self.assertEqual(QQInbound._parse("/reset"), ("reset", ""))
        self.assertEqual(classify("/reset")["intent"], "reset")

    def test_audit_does_not_store_full_context(self):
        from src.qq.threads import ThreadStore

        threads = ThreadStore(self.root / "threads.db")
        threads.append_turn(sender=7, question="秘密问题", answer="秘密答案", message_id="old")
        self.enqueue("继续")
        self.pending("1", intent="ask", params={"question": "继续"})

        with patch.object(self.handler, "reply_for", return_value="ok"):
            TaskWorker(
                outbox=self.outbox, client=self.client, handler=self.handler, threads=threads
            ).run_once()

        audit_text = str(self.handler.audit)
        self.assertNotIn("秘密答案", audit_text)
        self.assertNotIn("秘密问题", audit_text)



    def test_continue_run_success(self):
        self.enqueue("/continue 12345678-1234-1234-1234-123456789012 再补一段")
        self.pending(intent="continue_run")

        with patch(
            "src.qq.worker.continue_run",
            return_value={"run_id": "12345678-1234-1234-1234-123456789012", "revision": 2, "state": "succeeded", "artifacts": ["a.txt"]},
        ):
            self.task_worker().run_once()

        self.assertIn("已继续 12345678", self.client.sent[0][1])
        self.assertIn("revision 2", self.client.sent[0][1])
        self.assertEqual(self.outbox.get("1")["state"], "sent")

    def test_continue_run_failure_safe_reply(self):
        from src.qq.collab_continue import ContinueError

        self.enqueue("/continue 12345678-1234-1234-1234-123456789012 再补一段")
        self.pending(intent="continue_run")

        with patch("src.qq.worker.continue_run", side_effect=ContinueError("not_succeeded")):
            self.task_worker().run_once()

        self.assertIn("只有已成功的任务可以继续", self.client.sent[0][1])
        self.assertNotIn("再补一段", self.client.sent[0][1])



    def test_continue_run_counts_manifest(self):
        self.enqueue("/continue 12345678-1234-1234-1234-123456789012 再补一段")
        self.pending(intent="continue_run")

        with patch(
            "src.qq.worker.continue_run",
            return_value={
                "run_id": "12345678-1234-1234-1234-123456789012",
                "revision": 5,
                "state": "succeeded",
                "manifest": {"path": "x/r5/result.md", "sha256": "a", "bytes": 40},
            },
        ):
            self.task_worker().run_once()

        self.assertIn("产物 1 个", self.client.sent[0][1])


if __name__ == "__main__":
    unittest.main()
