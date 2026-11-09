"""QQ 双 Worker：control（只改 DB）与 task（路由 + 执行 + 发送）。

线性化点：

- `claim()` 原子认领（pending → running）；
- `claim_send()` 原子发送认领（running → sending，且 cancelled=0）；
- 取消发生在认领之前 → 认领失败、不发送；发生在 sending 之后 → 不承诺撤回。
"""

from __future__ import annotations

import json
import os
import threading
import time

from pathlib import Path

from src.qq import router
from src.qq.inbound import QQInbound, MAX_REPLY_CHARS
from src.qq.onebot import OneBotError
from src.qq.outbox import QQOutbox
from src.qq.policy import (
    ensure_workspace,
    generate_confirmation_id,
    params_hash,
    truncate_for_qq,
)


DENY_TEXT = "QQ 端不支持执行命令。"
UNAVAILABLE_TEXT = "QQ 端暂未开放出图/写文件。"
CLARIFY_TEXT = "请再说清楚一点：可以问问题，或用 /status、/ask、/cancel。"


class BaseWorker:
    def __init__(self, *, outbox: QQOutbox, client, handler: QQInbound, poll_interval: float = 0.5):
        self.outbox = outbox
        self.client = client
        self.handler = handler
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=self.__class__.__name__, daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()

        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self.poll_interval)

    def _run_confirmed_read(self, job: dict) -> bool:
        from src.qq.policy import PolicyError, validate_read_path

        message_id = job["message_id"]
        params = json.loads(job.get("params_json") or "{}")
        workspace = ensure_workspace(os.getenv("PA_QQ_WORKSPACE") or Path(".").resolve())

        try:
            target = validate_read_path(params.get("path", ""), workspace)
            content = target.read_text(encoding="utf-8", errors="replace")
        except (PolicyError, OSError) as error:
            self.reply(job, f"读取失败：{error}")

            return True

        self.outbox.set_routing(
            message_id,
            intent="read_file",
            params=params,
            params_hash=params_hash(params),
            decision_source="confirmed",
            state="running",
            policy_decision="allow",
        )
        self.reply(job, truncate_for_qq(content, MAX_REPLY_CHARS))

        return True

    def run_once(self) -> bool:  # pragma: no cover - 抽象
        raise NotImplementedError

    # ---------- 发送（含发送认领） ----------

    def notice(self, job: dict, text: str) -> bool:
        """发送提示，不改变业务状态（用于确认提示/拒绝说明）。"""

        try:
            self.client.send_private_msg(int(job["sender"]), text)
        except OneBotError as error:
            self.handler.record_audit(job, "send-failed", str(error))

            return False

        self.handler.record_audit(job, "notice-sent")

        return True

    def reply(self, job: dict, text: str) -> bool:
        message_id = job["message_id"]

        if not self.outbox.claim_send(message_id):
            self.handler.record_audit(job, "cancelled_before_send")
            self.outbox.mark_failed(message_id, error="cancelled before send")

            return False

        try:
            self.client.send_private_msg(int(job["sender"]), text)
        except OneBotError as error:
            self.outbox.mark_failed(message_id, error=str(error))
            self.handler.record_audit(job, "send-failed", str(error))

            return False

        self.outbox.mark_sent(message_id, reply=text)
        self.handler.record_audit(job, "sent")

        return True


class TaskWorker(BaseWorker):
    def _run_confirmed_read(self, job: dict) -> bool:
        from src.qq.policy import PolicyError, validate_read_path

        message_id = job["message_id"]
        params = json.loads(job.get("params_json") or "{}")
        workspace = ensure_workspace(os.getenv("PA_QQ_WORKSPACE") or Path(".").resolve())

        try:
            target = validate_read_path(params.get("path", ""), workspace)
            content = target.read_text(encoding="utf-8", errors="replace")
        except (PolicyError, OSError) as error:
            self.reply(job, f"读取失败：{error}")

            return True

        self.outbox.set_routing(
            message_id,
            intent="read_file",
            params=params,
            params_hash=params_hash(params),
            decision_source="confirmed",
            state="running",
            policy_decision="allow",
        )
        self.reply(job, truncate_for_qq(content, MAX_REPLY_CHARS))

        return True

    def run_once(self) -> bool:
        job = self.outbox.claim("task")

        if job is None:
            return False

        message_id = job["message_id"]

        # 已确认的任务：直接按存下来的 intent/params 执行，不重新路由
        if int(job.get("confirm_used") or 0) == 1 and job.get("intent") == "read_file":
            return self._run_confirmed_read(job)

        raw = job.get("raw_text") or ""
        decision = router.classify(raw)

        self.outbox.set_routing(
            message_id,
            intent=decision["intent"],
            params=decision["params"],
            params_hash=params_hash(decision["params"]),
            decision_source=decision["source"],
            state="running",
            policy_decision=decision["action"],
        )

        action = decision["action"]

        if action == "deny":
            self.outbox.set_routing(
                message_id,
                intent=decision["intent"],
                params={},
                params_hash=params_hash({}),
                decision_source=decision["source"],
                state="denied",
                policy_decision="deny",
            )
            self.notice(job, DENY_TEXT)

            return True

        if action == "unavailable":
            self.notice(job, UNAVAILABLE_TEXT)

            return True

        if action == "clarify":
            self.notice(job, CLARIFY_TEXT)

            return True

        if action == "confirm":
            workspace = ensure_workspace(
                os.getenv("PA_QQ_WORKSPACE") or Path(".").resolve()
            )
            from src.qq.policy import decide

            policy = decide(
                intent=decision["intent"],
                params=decision["params"],
                route_action="confirm",
                workspace=workspace,
            )

            if policy["decision"] != "confirm":
                self.reply(job, f"不能读取：{policy['reason']}")

                return True

            self.outbox.set_awaiting_confirm(
                message_id,
                confirm_id=policy["confirm_id"],
                expires_at=time.time() + policy["ttl"],
            )
            text = (
                f"要读取 {decision['params']['path']} 吗？"
                f"回复 /confirm {policy['confirm_id']}（{policy['ttl']} 秒内有效）"
            )
            self.notice(job, text)

            return True

        # allow
        if decision["intent"] == "status":
            text = "Personal Agent 在线；QQ 入站已接通。"
        elif decision["intent"] == "ask":
            text = self.handler.reply_for("ask", decision["params"].get("question", ""))
        else:
            text = DENY_TEXT

        text = truncate_for_qq(text, MAX_REPLY_CHARS)
        self.outbox.set_routing(
            message_id,
            intent=decision["intent"],
            params=decision["params"],
            params_hash=params_hash(decision["params"]),
            decision_source=decision["source"],
            state="running",
            policy_decision="allow",
        )
        self.reply(job, text)

        return True


class ControlWorker(BaseWorker):
    def _run_confirmed_read(self, job: dict) -> bool:
        from src.qq.policy import PolicyError, validate_read_path

        message_id = job["message_id"]
        params = json.loads(job.get("params_json") or "{}")
        workspace = ensure_workspace(os.getenv("PA_QQ_WORKSPACE") or Path(".").resolve())

        try:
            target = validate_read_path(params.get("path", ""), workspace)
            content = target.read_text(encoding="utf-8", errors="replace")
        except (PolicyError, OSError) as error:
            self.reply(job, f"读取失败：{error}")

            return True

        self.outbox.set_routing(
            message_id,
            intent="read_file",
            params=params,
            params_hash=params_hash(params),
            decision_source="confirmed",
            state="running",
            policy_decision="allow",
        )
        self.reply(job, truncate_for_qq(content, MAX_REPLY_CHARS))

        return True

    def run_once(self) -> bool:
        job = self.outbox.claim("control")

        if job is None:
            return False

        raw = (job.get("raw_text") or "").strip()
        decision = router.classify(raw)

        if decision["intent"] == "confirm":
            confirm_id = decision["params"].get("confirmation_id", "")

            if not confirm_id:
                text = "请带上确认码：/confirm <确认码>"
            else:
                consumed = self.outbox.consume_confirm(
                    sender=int(job["sender"]), confirm_id=confirm_id
                )

                if consumed is None:
                    text = "确认已失效或不存在。"
                else:
                    text = "已确认，开始执行。"

            self.reply(job, text)

            return True

        if decision["intent"] == "cancel":
            outcome, target = self.outbox.cancel(
                sender=int(job["sender"]), target=decision["params"].get("target", "")
            )

            if outcome == "cancelled":
                text = f"已取消：{target['message_id'] if target else ''}"
            elif outcome == "running":
                text = "已取消结果发送；当前调用可能无法中途停止，仍可能产生费用。"
            elif outcome == "sending":
                text = "结果发送已开始，无法保证取消。"
            else:
                text = "找不到该任务（可能已完成或不是你的）。"

            self.reply(job, text)

            return True

        self.reply(job, CLARIFY_TEXT)

        return True


def build_workers(*, db_path: str, client, handler: QQInbound):
    outbox = QQOutbox(db_path)
    outbox.recover_inflight()

    task = TaskWorker(outbox=outbox, client=client, handler=handler)
    control = ControlWorker(outbox=outbox, client=client, handler=handler)

    return outbox, task, control
