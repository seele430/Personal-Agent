"""QQ 出站 worker：单线程消费队列，串行执行命令并发送。

规则：

- 只有 worker 调用 Provider 与 `send_msg`，端点只负责入队；
- 发送失败（明确的 OneBotError）→ `failed`，**不重试**；
- 超时等结果未知 → `unknown`，**不重发**；
- 同一 `message_id` 只会被处理一次（队列主键 + claim）。
"""

from __future__ import annotations

import threading
import time

from src.qq.inbound import MAX_QUESTION_CHARS, MAX_REPLY_CHARS
from src.qq.onebot import OneBotError
from src.qq.outbox import QQOutbox


class QQWorker:
    def __init__(self, *, outbox: QQOutbox, client, handler, poll_interval: float = 0.5):
        self.outbox = outbox
        self.client = client
        self.handler = handler
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self.outbox.recover_inflight()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="qq-outbox-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()

        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ---------- 主循环 ----------

    def _loop(self) -> None:
        while not self._stop.is_set():
            processed = self.run_once()

            if not processed:
                self._stop.wait(self.poll_interval)

    def run_once(self) -> bool:
        job = self.outbox.claim_next()

        if job is None:
            return False

        message_id = job["message_id"]

        try:
            reply = self.handler.reply_for(job["command"], job.get("question") or "")

        except Exception as error:  # noqa: BLE001 - 任务失败不能拖垮 worker
            self.outbox.mark_failed(message_id, error=f"command failed: {error}")
            self.handler.record_audit(job, "failed", str(error))

            return True

        if not reply:
            self.outbox.mark_failed(message_id, error="empty reply")
            self.handler.record_audit(job, "failed", "empty reply")

            return True

        try:
            result = self.client.send_private_msg(int(job["sender"]), reply)

        except OneBotError as error:
            self.outbox.mark_failed(message_id, error=str(error))
            self.handler.record_audit(job, "send-failed", str(error))

            return True

        self.outbox.mark_sent(
            message_id,
            reply=reply,
            result_message_id=str((result or {}).get("message_id") or ""),
        )
        self.handler.record_audit(job, "sent")

        return True


def build_worker(*, db_path: str, client, handler, poll_interval: float = 0.5) -> QQWorker:
    return QQWorker(
        outbox=QQOutbox(db_path),
        client=client,
        handler=handler,
        poll_interval=poll_interval,
    )
