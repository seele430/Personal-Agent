"""M1 + M2 review bus tests。"""

import json
import os
import tempfile
import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.collab import review_bus


class ReviewBusCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        (self.root / "kimi" / "outbox").mkdir(parents=True)
        (self.root / "state").mkdir(parents=True)
        self.set_state(round=1, status="waiting_kimi")

    def set_state(self, **overrides):
        state = {
            "round": 1,
            "status": "waiting_kimi",
            "history": [],
            **overrides,
        }
        (self.root / "state" / "review.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )

    def result(self, payload, name="round-001-result.json"):
        path = self.root / "kimi" / "outbox" / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        return path

    def valid(self, verdict="pass", findings=None, **extra):
        payload = {
            "round": 1,
            "verdict": verdict,
            "findings": findings or [],
        }
        payload.update(extra)

        return payload

    def request(self, round_number, text="## 旧请求\n"):
        path = self.root / "kimi" / "inbox" / f"round-{round_number:03d}-request.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

        return path

    def draft(self, round_number, text=None):
        path = review_bus.draft_path(self.root, round_number)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            text
            or (
                f"# 复审请求 round-{round_number:03d}（草稿）\n\n"
                "> 状态：**草稿**——本文件位于 `pa/outbox/drafts/`，移到 `kimi/inbox/` 才算发送；"
                "发送前必须人工确认。\n\n"
                "## 本轮修了什么\n- 修了 blocker\n- 补了测试\n\n"
                "## 请复审\n- 目标文件：plans/x.md\n- 范围：全文\n"
                "- 要求：按结果合同输出 JSON，每条 finding 带 evidence。\n"
            ),
            encoding="utf-8",
        )

        return path

    def finding(self, severity="high", **overrides):
        item = {
            "severity": severity, "file": "a.py", "line": 1,
            "issue": "x", "evidence": "y", "suggestion": "z",
        }
        item.update(overrides)

        return item

    # ---------------------------------------------------------------- M1

    def test_pass_advances_and_archives(self):
        self.result(self.valid())
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "processed")
        state = review_bus.load_state(self.root)
        self.assertEqual(state["status"], "pass_waiting_human_signoff")
        self.assertTrue((self.root / "archive" / "round-001" / "round-001-result.json").is_file())
        self.assertEqual(len(state["processed_results"]), 1)

    def test_findings_advance_to_fixing(self):
        self.result(self.valid("findings", [self.finding()]))
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "processed")
        self.assertEqual(review_bus.load_state(self.root)["status"], "fixing")

    def test_duplicate_ignored(self):
        self.result(self.valid())
        review_bus.process_once(self.root)
        state = review_bus.load_state(self.root)
        state["status"] = "waiting_kimi"
        review_bus.save_state_atomic(self.root, state)
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "duplicate")

    def test_round_mismatch_rejected(self):
        self.result({"round": 2, "verdict": "pass", "findings": []})
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "review_failed")
        self.assertIn("round_mismatch", result["errors"][0])

    def test_invalid_json_rejected(self):
        (self.root / "kimi" / "outbox" / "round-001-result.json").write_text(
            "{not json", encoding="utf-8"
        )
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "review_failed")
        self.assertIn("invalid_json", result["errors"][0])

    def test_pass_with_findings_rejected(self):
        self.result(self.valid("pass", [self.finding()]))
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "review_failed")
        self.assertIn("pass_with_findings", result["errors"][0])

    def test_missing_evidence_rejected(self):
        finding = {"severity": "high", "file": "a.py", "line": 1, "issue": "x"}
        self.result(self.valid("findings", [finding]))
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "review_failed")
        self.assertIn("missing_evidence", result["errors"][0])

    def test_too_large_rejected(self):
        path = self.root / "kimi" / "outbox" / "round-001-result.json"
        path.write_bytes(b"{" + b" " * (review_bus.MAX_RESULT_BYTES + 1) + b"}")
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "review_failed")
        self.assertIn("result_too_large", result["errors"][0])

    def test_no_result(self):
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "no_result")

    def test_state_survives_atomic_write(self):
        self.result(self.valid("findings", []))
        review_bus.process_once(self.root)
        raw = (self.root / "state" / "review.json").read_text(encoding="utf-8")
        self.assertIsInstance(json.loads(raw), dict)

    def test_signed_off_not_reprocessed(self):
        self.result(self.valid())
        self.set_state(round=1, status="signed_off")
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "not_waiting")
        self.assertEqual(review_bus.load_state(self.root)["status"], "signed_off")

    # ---------------------------------------------------------------- M2-2/3

    def test_findings_write_fix_task_and_draft(self):
        self.result(self.valid("findings", [self.finding("blocker")], target="plans/x.md"))
        review_bus.process_once(self.root)
        state = review_bus.load_state(self.root)

        self.assertEqual(state["status"], "fixing")
        self.assertEqual(state["loop_round"], 1)
        self.assertTrue(review_bus.fix_task_path(self.root, 1).is_file())
        self.assertTrue(review_bus.draft_path(self.root, 2).is_file())
        self.assertIn("blocker", review_bus.fix_task_path(self.root, 1).read_text(encoding="utf-8"))

    def test_findings_never_write_kimi_inbox(self):
        self.result(self.valid("findings", [self.finding()]))
        review_bus.process_once(self.root)
        inbox = self.root / "kimi" / "inbox"
        self.assertFalse(inbox.exists() and any(inbox.iterdir()))

    def test_third_round_findings_stop_without_draft(self):
        self.set_state(round=2, status="waiting_kimi", loop_round=2)
        self.result(
            self.valid("findings", [self.finding()], round=2),
            name="round-002-result.json",
        )
        result = review_bus.process_once(self.root)
        state = review_bus.load_state(self.root)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(state["loop_round"], 3)
        self.assertFalse(review_bus.draft_path(self.root, 3).exists())

    # ---------------------------------------------------------------- M2-4

    def test_confirm_send_moves_draft_and_advances_round(self):
        self.set_state(round=1, status="fixing", target="plans/x.md")
        self.draft(2)
        result = review_bus.confirm_send(self.root, 2)
        state = review_bus.load_state(self.root)

        self.assertTrue(result["ok"])
        self.assertEqual(state["round"], 2)
        self.assertEqual(state["status"], "waiting_kimi")
        self.assertEqual(state["request_file"], "round-002-request.md")
        self.assertIsNone(state["draft_file"])
        self.assertFalse(review_bus.draft_path(self.root, 2).exists())
        self.assertTrue(review_bus.inbox_request_path(self.root, 2).is_file())

    def test_confirm_send_marks_request_as_sent(self):
        self.set_state(round=1, status="fixing")
        self.draft(2)
        review_bus.confirm_send(self.root, 2)
        text = review_bus.inbox_request_path(self.root, 2).read_text(encoding="utf-8")

        self.assertIn("**已发送**", text)
        self.assertNotIn("状态：**草稿**", text)
        self.assertNotIn("（草稿）", text)

    def test_confirm_send_requires_fixing(self):
        self.set_state(round=1, status="waiting_kimi")
        self.draft(2)
        result = review_bus.confirm_send(self.root, 2)

        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "not_fixing")
        self.assertTrue(review_bus.draft_path(self.root, 2).is_file())

    def test_confirm_send_rejects_existing_target(self):
        self.set_state(round=1, status="fixing")
        self.draft(2)
        self.request(2)
        result = review_bus.confirm_send(self.root, 2)

        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "target_exists")

    def test_confirm_send_rejects_tiny_draft(self):
        self.set_state(round=1, status="fixing")
        self.draft(2, text="太短")
        result = review_bus.confirm_send(self.root, 2)

        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "draft_invalid")
        self.assertIn("draft_too_small", result["errors"])

    def test_confirm_send_timeout_starts_at_send_time(self):
        """草稿放旧了再发，不能把等待确认的时间算进超时。"""

        self.set_state(round=1, status="fixing")
        path = self.draft(2)
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        stamp = old.timestamp()
        os.utime(path, (stamp, stamp))

        review_bus.confirm_send(self.root, 2)
        state = review_bus.load_state(self.root)
        sent_at = datetime.fromisoformat(state["request_sent_at"])

        self.assertLess((datetime.now(timezone.utc) - sent_at).total_seconds(), 60)
        timeout = review_bus.check_timeout(self.root, timeout=600)
        self.assertEqual(timeout["action"], "waiting")

    def test_reconcile_recovers_manual_request(self):
        self.set_state(round=1, status="fixing")
        self.request(2, "# 手放进去的请求\n## 请复审\n")
        result = review_bus.reconcile(self.root)
        state = review_bus.load_state(self.root)

        self.assertEqual(result["action"], "recovered")
        self.assertEqual(state["round"], 2)
        self.assertEqual(state["status"], "waiting_kimi")
        self.assertTrue(
            any(item["event"] == "request_sent_recovered" for item in state["history"])
        )

    # ---------------------------------------------------------------- M2-5

    def test_notify_once_per_round_event(self):
        calls = []

        def notifier(text):
            calls.append(text)

        self.result(self.valid())
        review_bus.process_once(self.root, notifier=notifier)
        state = review_bus.load_state(self.root)
        self.assertEqual(len(calls), 1)
        self.assertIn("1:pass", state["notified"])

        state["status"] = "waiting_kimi"
        review_bus.save_state_atomic(self.root, state)
        review_bus.process_once(self.root, notifier=notifier)
        self.assertEqual(len(calls), 1)

    def test_pass_archives_request(self):
        self.request(1, "# 请求\n## 请复审\n")
        self.result(self.valid())
        review_bus.process_once(self.root)
        self.assertTrue(
            (self.root / "archive" / "round-001" / "round-001-request.md").is_file()
        )

    # ---------------------------------------------------------------- M2-6

    def test_timeout_stops_and_notifies(self):
        calls = []
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.set_state(round=2, status="waiting_kimi", request_sent_at=old)
        self.request(2, "# 请求\n## 请复审\n")
        result = review_bus.check_timeout(
            self.root, timeout=60, notifier=calls.append
        )

        self.assertEqual(result["action"], "stopped")
        self.assertEqual(review_bus.load_state(self.root)["status"], "stopped")
        self.assertEqual(len(calls), 1)
        self.assertTrue(
            (self.root / "archive" / "round-002" / "round-002-request.md").is_file()
        )

    def test_timeout_waits_before_deadline(self):
        fresh = datetime.now(timezone.utc).isoformat()
        self.set_state(round=2, status="waiting_kimi", request_sent_at=fresh)
        result = review_bus.check_timeout(self.root, timeout=600)

        self.assertEqual(result["action"], "waiting")
        self.assertGreater(result["remaining"], 0)

    def test_timeout_ignores_draft_waiting(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.set_state(round=2, status="fixing", request_sent_at=old)
        result = review_bus.check_timeout(self.root, timeout=60)

        self.assertEqual(result["action"], "not_waiting")

    def test_manual_stop_archives_request(self):
        self.set_state(round=2, status="waiting_kimi")
        self.request(2, "# 请求\n## 请复审\n")
        result = review_bus.stop(self.root, "manual")

        self.assertEqual(result["action"], "stopped")
        self.assertTrue(
            (self.root / "archive" / "round-002" / "round-002-request.md").is_file()
        )


if __name__ == "__main__":
    unittest.main()
