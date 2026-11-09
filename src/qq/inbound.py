"""QQ 入站：OneBot 事件 → 白名单校验 → 只读命令 → 审计。

安全约束（写死）：

- **只认 OWNER 一个 QQ 号**：其他人的私聊**丢弃、不入库、不回复**；
- 只处理 `post_type=message` + `message_type=private`；
- HMAC-SHA1 签名校验（常量时间比较），没签名直接 401；
- 同一 `message_id` 只处理一次；
- 每人每分钟最多 `MAX_PER_MINUTE` 条，超出丢弃；
- 只支持 `/status`，其它指令回「不支持」——QQ 端不能触发任何写操作；
- 审计只存 80 字摘要，不存消息全文。
"""

from __future__ import annotations

import hashlib
import hmac
import time

from src.qq.onebot import OneBotClient, OneBotError


MAX_PER_MINUTE = 10
AUDIT_SUMMARY_LIMIT = 80


class QQInbound:
    def __init__(
        self,
        *,
        owner_id: int,
        secret: str,
        client: OneBotClient,
        enabled: bool = True,
        max_per_minute: int = MAX_PER_MINUTE,
        clock=None,
    ):
        self.owner_id = int(owner_id)
        self.secret = (secret or "").strip()
        self.client = client
        self.enabled = enabled
        self.max_per_minute = max_per_minute
        self._clock = clock or time.time
        self._seen: dict[str, float] = {}
        self._recent: dict[int, list[float]] = {}
        self.audit: list[dict] = []

    # ---------- 签名 ----------

    def verify(self, body: bytes, signature: str) -> bool:
        """接受 `sha1=` / `sha256=` 前缀或裸 hex；NapCat 用 sha256。"""

        if not self.secret:
            return False

        provided = (signature or "").strip()

        if not provided:
            return False

        key = self.secret.encode("utf-8")
        sha1_hex = hmac.new(key, body, hashlib.sha1).hexdigest()
        sha256_hex = hmac.new(key, body, hashlib.sha256).hexdigest()

        if provided.startswith("sha1="):
            return hmac.compare_digest(provided[5:], sha1_hex)

        if provided.startswith("sha256="):
            return hmac.compare_digest(provided[7:], sha256_hex)

        return hmac.compare_digest(provided, sha1_hex) or hmac.compare_digest(
            provided, sha256_hex
        )

    # ---------- 事件 ----------

    def handle(self, event: dict) -> tuple[int, dict]:
        if not self.enabled:
            return 204, {}

        if event.get("post_type") != "message":
            return 204, {}

        if event.get("message_type") != "private":
            return 204, {}

        sender = event.get("sender") or {}
        sender_id = int(sender.get("user_id") or event.get("user_id") or 0)

        # 白名单：不是本人，直接丢弃——不入库、不回复
        if sender_id != self.owner_id:
            self._record(sender_id, event, "dropped:not-owner")
            return 204, {}

        message_id = str(event.get("message_id") or "")

        if message_id and message_id in self._seen:
            return 204, {}

        if message_id:
            self._seen[message_id] = self._clock()

        if not self._allow(sender_id):
            self._record(sender_id, event, "dropped:rate-limit")
            return 204, {}

        text = self._text_of(event)
        reply = self._reply_for(text)

        try:
            self.client.send_private_msg(sender_id, reply)
            self._record(sender_id, event, "replied")
        except OneBotError as error:
            self._record(sender_id, event, f"reply-failed:{error}")

        return 200, {"reply": reply}

    # ---------- 内部 ----------

    def _allow(self, sender_id: int) -> bool:
        now = self._clock()
        window = [t for t in self._recent.get(sender_id, []) if now - t < 60]
        self._recent[sender_id] = window

        if len(window) >= self.max_per_minute:
            return False

        window.append(now)

        return True

    @staticmethod
    def _text_of(event: dict) -> str:
        raw = event.get("message")

        if isinstance(raw, str):
            return raw

        if isinstance(raw, list):
            parts = [
                str(segment.get("data", {}).get("text") or "")
                for segment in raw
                if isinstance(segment, dict) and segment.get("type") == "text"
            ]

            return "".join(parts)

        return str(event.get("raw_message") or "")

    @staticmethod
    def _reply_for(text: str) -> str:
        command = (text or "").strip()

        if command in ("/status", "status"):
            return "Personal Agent 在线；QQ 入站已接通。"

        return "仅支持 /status。"

    def _record(self, sender_id: int, event: dict, action: str) -> None:
        self.audit.append(
            {
                "ts": self._clock(),
                "sender": sender_id,
                "message_id": str(event.get("message_id") or ""),
                "action": action,
                "summary": self._text_of(event)[:AUDIT_SUMMARY_LIMIT],
            }
        )
