"""OneBot 11 HTTP 客户端：只做「发消息」与「查登录态」。

约束：

- 只连 127.0.0.1 上的 NapCat，不对外；
- 超时 10 秒，失败抛 `OneBotError`（中文短句），不自动重试；
- token 只在请求头里出现，不进日志。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


DEFAULT_TIMEOUT = 10.0


class OneBotError(RuntimeError):
    """OneBot 调用失败。"""


class OneBotClient:
    def __init__(self, *, base_url: str, token: str, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = (base_url or "").rstrip("/")
        self.token = (token or "").strip()
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _call(self, action: str, payload: dict | None = None) -> dict:
        result = self._call_raw(action, payload)
        self._audit(action, result)

        return result

    def _audit(self, action: str, result: dict) -> None:
        import os

        if (os.getenv("PA_QQ_AUDIT") or "").strip() != "1":
            return

        try:
            import json
            from pathlib import Path as _Path

            root = _Path(os.getenv("PERSONAL_AGENT_DATA_DIR") or _Path(__file__).resolve().parents[2] / "data")
            root.mkdir(parents=True, exist_ok=True)

            with (root / "qq-audit.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "action": "outbound",
                            "api": action,
                            "message_id": str((result or {}).get("message_id") or ""),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass

    def _call_raw(self, action: str, payload: dict | None = None) -> dict:
        if not self.configured:
            raise OneBotError("未配置 PA_ONEBOT_URL / PA_ONEBOT_TOKEN")

        request = urllib.request.Request(
            f"{self.base_url}/{action}",
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")

        except urllib.error.HTTPError as error:
            raise OneBotError(f"OneBot 返回 HTTP {error.code}") from None

        except (urllib.error.URLError, OSError) as error:
            raise OneBotError(f"连不上 OneBot（{self.base_url}）：{error}") from None

        try:
            body = json.loads(raw)

        except json.JSONDecodeError:
            raise OneBotError("OneBot 返回的不是 JSON") from None

        if body.get("status") != "ok":
            raise OneBotError(f"OneBot 调用失败：{body.get('message') or body.get('wording') or 'unknown'}")

        return body.get("data") or {}

    def send_private_msg(self, user_id: int, text: str) -> dict:
        if not str(text or "").strip():
            raise OneBotError("消息内容不能为空")

        # 实测 NapCat 4.18.41 下 send_private_msg 在“事件处理中回复”场景会重复发送，
        # send_msg 只发一次；两者语义等价（private + user_id）。
        return self._call(
            "send_msg",
            {
                "message_type": "private",
                "user_id": int(user_id),
                "message": str(text),
            },
        )

    def send_private_image(self, user_id: int, file_path: str) -> dict:
        """发本地图片：file:// URI 由 NapCat 在本机读取，不走网络上传。"""

        from pathlib import Path

        path = Path(file_path).resolve()

        if not path.is_file():
            raise OneBotError(f"图片文件不存在：{path.name}")

        return self._call(
            "send_msg",
            {
                "message_type": "private",
                "user_id": int(user_id),
                "message": [
                    {"type": "image", "data": {"file": path.as_uri()}},
                ],
            },
        )

    def get_login_info(self) -> dict:
        return self._call("get_login_info")
