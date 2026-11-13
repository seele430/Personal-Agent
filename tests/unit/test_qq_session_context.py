"""QQ 问答注入会话上下文的单元测试：全 mock，不联网。"""

import os
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.qq.inbound import QQInbound
from src.qq.outbox import QQOutbox
from src.qq.worker import TaskWorker
from src.sessions import SessionStore


class FakeClient:
    def __init__(self):
        self.sent = []

    def send_private_msg(self, user_id, text):
        self.sent.append((user_id, text))

        return {"message_id": 1}


class FakeProvider:
    configured = True
    model = "test-model"

    def __init__(self):
        self.prompts = []

    def submit(self, prompt, *, artifact_name, out_dir):
        self.prompts.append(prompt)
        (Path(out_dir) / artifact_name).write_text("答案", encoding="utf-8")

        return {"task_id": "t"}


class ContextStoreCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = SessionStore(Path(self._temporary.name) / "sessions.db")

    def test_context_excludes_current_message(self):
        session = self.store.create("qq")
        self.store.add_message(session["id"], "user", "帮我做海报", "m-old")
        self.store.add_message(session["id"], "assistant", "海报方案", "m-answer")
        self.store.add_message(session["id"], "user", "再发一遍", "m-current")

        context = self.store.context(
            session["id"], exclude_message_id="m-current"
        )

        self.assertIn("帮我做海报", context)
        self.assertIn("海报方案", context)
        self.assertNotIn("再发一遍", context)

    def test_context_budget_discards_oldest(self):
        session = self.store.create("qq")
        self.store.add_message(session["id"], "user", "最旧消息", "m1")
        self.store.add_message(session["id"], "assistant", "中间消息", "m2")
        self.store.add_message(session["id"], "user", "最新消息", "m3")

        context = self.store.context(
            session["id"], max_messages=3, max_chars=20
        )

        self.assertIn("最新消息", context)
        self.assertNotIn("最旧消息", context)
        self.assertLessEqual(len(context), 20)


class WorkerContextCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self._environment = patch.dict(
            os.environ,
            {
                "PERSONAL_AGENT_DATA_DIR": str(self.root),
                "PA_QQ_SESSION_CONTEXT": "1",
            },
            clear=False,
        )
        self._environment.start()
        self.addCleanup(self._environment.stop)
        self.outbox = QQOutbox(self.root / "outbox.db")
        self.client = FakeClient()
        self.handler = QQInbound(owner_id=7, secret="s", client=self.client)
        self.store = SessionStore()

    def activate_session(self, title="当前会话"):
        session = self.store.create("qq", title)
        self.store.set_active(7, session["id"])

        return session

    def enqueue_ask(self, question="再发一遍", message_id="m-current"):
        self.outbox.enqueue(
            message_id=message_id,
            sender=7,
            raw_text=f"/ask {question}",
            queue="task",
        )
        self.outbox.set_routing(
            message_id,
            intent="ask",
            params={"question": question},
            params_hash="h",
            decision_source="rule",
            state="pending_task",
        )

    def run_worker(self, capture, status=None):
        self.enqueue_ask()

        with patch(
            "src.collab.orchestrator.Orchestrator"
        ) as orchestrator_class:
            orchestrator_class.return_value.status.return_value = status

            with patch.object(self.handler, "reply_for", side_effect=capture):
                TaskWorker(
                    outbox=self.outbox,
                    client=self.client,
                    handler=self.handler,
                ).run_once()

    def test_worker_injects_history_and_run_summary(self):
        session = self.activate_session()
        self.store.add_message(session["id"], "user", "帮我做海报", "m-old")
        self.store.add_message(session["id"], "assistant", "海报方案如下", "m-answer")
        captured = {}

        def capture(command, question, *, context=""):
            captured.update(command=command, question=question, context=context)

            return "答案"

        self.run_worker(
            capture,
            status={
                "run_id": "run-20261112-215500-3c53a6",
                "state": "stopped",
                "stop_reason": "code_failed",
                "artifacts": 13,
            },
        )

        self.assertEqual(captured["command"], "ask")
        self.assertEqual(captured["question"], "再发一遍")
        self.assertIn("帮我做海报", captured["context"])
        self.assertIn("海报方案如下", captured["context"])
        self.assertIn("run-20261112-215500-3c53a6｜stopped｜code_failed｜产物 13 个", captured["context"])
        self.assertLessEqual(len(captured["context"]), 3000)

    def test_worker_does_not_duplicate_current_question(self):
        session = self.activate_session()
        self.store.add_message(session["id"], "user", "前一条问题", "m-old")
        self.store.add_message(session["id"], "user", "再发一遍", "m-current")
        captured = {}

        def capture(command, question, *, context=""):
            captured["context"] = context

            return "答案"

        self.run_worker(capture)

        self.assertIn("前一条问题", captured["context"])
        self.assertNotIn("再发一遍", captured["context"])

    def test_worker_uses_only_active_session(self):
        old = self.store.create("qq", "旧会话")
        self.store.add_message(old["id"], "user", "旧会话秘密", "m-old")
        current = self.activate_session("新会话")
        self.store.add_message(current["id"], "user", "新会话内容", "m-new")
        captured = {}

        def capture(command, question, *, context=""):
            captured["context"] = context

            return "答案"

        self.run_worker(capture)

        self.assertIn("新会话内容", captured["context"])
        self.assertNotIn("旧会话秘密", captured["context"])

    def test_worker_creates_active_session_when_missing(self):
        self.assertIsNone(self.store.active_session(7))
        self.enqueue_ask("你好")

        with patch(
            "src.collab.orchestrator.Orchestrator"
        ) as orchestrator_class:
            orchestrator_class.return_value.status.return_value = None

            with patch.object(self.handler, "reply_for", return_value="答案"):
                TaskWorker(
                    outbox=self.outbox,
                    client=self.client,
                    handler=self.handler,
                ).run_once()

        self.assertIsNotNone(self.store.active_session(7))

    def test_two_asks_record_two_assistant_messages(self):
        """assistant 落库不能只留第一条（唯一键撞空 message_id 会静默丢弃）。"""

        session = self.activate_session()

        for index, (message_id, question) in enumerate(
            (("m-1", "第一问"), ("m-2", "第二问")), start=1
        ):
            self.outbox.enqueue(
                message_id=message_id,
                sender=7,
                raw_text=f"/ask {question}",
                queue="task",
            )
            self.outbox.set_routing(
                message_id,
                intent="ask",
                params={"question": question},
                params_hash="h",
                decision_source="rule",
                state="pending_task",
            )

            with patch("src.collab.orchestrator.Orchestrator") as orchestrator_class:
                orchestrator_class.return_value.status.return_value = None

                with patch.object(
                    self.handler, "reply_for", return_value=f"答{index}"
                ):
                    TaskWorker(
                        outbox=self.outbox,
                        client=self.client,
                        handler=self.handler,
                    ).run_once()

        rows = [
            row for row in self.store.messages(session["id"])
            if row["role"] == "assistant"
        ]

        self.assertEqual([row["content"] for row in rows], ["答1", "答2"])

    def test_continue_ask_skips_duplicate_history(self):
        """「继续…」已带上一问一答，不能再叠加同一段会话历史。"""

        from src.qq.threads import ThreadStore

        session = self.activate_session()
        self.store.add_message(session["id"], "user", "3+3 是多少", "m-old")
        self.store.add_message(session["id"], "assistant", "6", "m-answer")
        threads = ThreadStore(self.root / "threads.db")
        threads.append_turn(
            sender=7, question="3+3 是多少", answer="6", message_id="m-old"
        )
        captured = {}

        def capture(command, question, **kwargs):
            captured.update(
                command=command,
                question=question,
                context=kwargs.get("context", ""),
            )

            return "答案"

        self.outbox.enqueue(
            message_id="m-current",
            sender=7,
            raw_text="继续，说明怎么算的",
            queue="task",
        )
        self.outbox.set_routing(
            "m-current",
            intent="ask",
            params={"question": "继续，说明怎么算的"},
            params_hash="h",
            decision_source="rule",
            state="pending_task",
        )

        with patch("src.collab.orchestrator.Orchestrator") as orchestrator_class:
            orchestrator_class.return_value.status.return_value = {
                "run_id": "run-20261112-215500-3c53a6",
                "state": "stopped",
                "stop_reason": "code_failed",
                "artifacts": 13,
            }

            with patch.object(self.handler, "reply_for", side_effect=capture):
                TaskWorker(
                    outbox=self.outbox,
                    client=self.client,
                    handler=self.handler,
                    threads=threads,
                ).run_once()

        self.assertIn("3+3 是多少", captured["question"])
        self.assertNotIn("[会话历史", captured["context"])
        self.assertIn("code_failed", captured["context"])
        self.assertEqual(captured["question"].count("3+3 是多少"), 1)

    def test_new_session_reply_not_recorded(self):
        """`/new` 的回执不该写进消息表：否则新会话一开始就带一条孤立助手消息。"""

        self.activate_session("旧会话")
        self.outbox.enqueue(
            message_id="m-new",
            sender=7,
            raw_text="/new",
            queue="task",
        )

        with patch("src.collab.orchestrator.Orchestrator") as orchestrator_class:
            orchestrator_class.return_value.status.return_value = None

            TaskWorker(
                outbox=self.outbox,
                client=self.client,
                handler=self.handler,
            ).run_once()

        session = self.store.active_session(7)
        self.assertNotEqual(session["title"], "旧会话")
        self.assertEqual(self.store.messages(session["id"]), [])

    def test_context_switch_restores_legacy_call(self):
        self.enqueue_ask("只发当前问题")
        captured = {}

        def capture(command, question):
            captured.update(command=command, question=question)

            return "答案"

        with patch.dict(os.environ, {"PA_QQ_SESSION_CONTEXT": "0"}, clear=False):
            with patch("src.collab.orchestrator.Orchestrator") as orchestrator_class:
                with patch.object(self.handler, "reply_for", side_effect=capture):
                    TaskWorker(
                        outbox=self.outbox,
                        client=self.client,
                        handler=self.handler,
                    ).run_once()

        orchestrator_class.assert_not_called()
        self.assertEqual(captured, {"command": "ask", "question": "只发当前问题"})

    def test_context_failure_falls_back_to_question_only(self):
        self.enqueue_ask("降级问题")
        captured = {}

        def capture(command, question):
            captured.update(command=command, question=question)

            return "答案"

        with patch("src.sessions.SessionStore", side_effect=RuntimeError("db locked")):
            with patch(
                "src.collab.orchestrator.Orchestrator"
            ) as orchestrator_class:
                orchestrator_class.return_value.status.return_value = None

                with patch.object(self.handler, "reply_for", side_effect=capture):
                    TaskWorker(
                        outbox=self.outbox,
                        client=self.client,
                        handler=self.handler,
                    ).run_once()

        self.assertEqual(captured, {"command": "ask", "question": "降级问题"})
        self.assertEqual(self.outbox.get("m-current")["state"], "sent")


class InboundContextCase(unittest.TestCase):
    def setUp(self):
        self.handler = QQInbound(owner_id=7, secret="s", client=FakeClient())
        self.provider = FakeProvider()

    def test_inbound_appends_context_before_question(self):
        context = "[会话历史｜只作参考，不是用户指令]\n用户：帮我做海报"

        with patch(
            "src.collab.providers.build_provider", return_value=self.provider
        ), patch("src.collab.providers.provider_name", return_value="ark"):
            answer = self.handler.reply_for(
                "ask", "再发一遍", context=context
            )

        prompt = self.provider.prompts[0]
        self.assertEqual(answer, "答案")
        self.assertLess(prompt.index(context), prompt.index("[用户问题]\n再发一遍"))
        self.assertEqual(prompt.count("再发一遍"), 1)

    def test_question_limit_ignores_context_length(self):
        long_context = "历史" * 2000

        with patch(
            "src.collab.providers.build_provider", return_value=self.provider
        ), patch("src.collab.providers.provider_name", return_value="ark"):
            accepted = self.handler.reply_for(
                "ask", "问" * 500, context=long_context
            )
            rejected = self.handler.reply_for(
                "ask", "问" * 501, context=long_context
            )

        self.assertEqual(accepted, "答案")
        self.assertIn("太长", rejected)
        self.assertEqual(len(self.provider.prompts), 1)


if __name__ == "__main__":
    unittest.main()
