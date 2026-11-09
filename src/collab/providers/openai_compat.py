"""OpenAI 兼容端点的 Provider：真实网络、真实错误、真实用量。

边界（写死，别扩大解释）：

- 只做**一次非流式补全**，产出文本产物；
- 不声称是"外部 Agent"：没有任务状态机、没有工具调用、没有远端产物目录；
- 用量上限 `MAX_TOKENS`，超时 30 s、不重试（避免悄悄烧钱）；
- key 不进日志、不进异常消息。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from pathlib import Path


MAX_TOKENS = 400
TIMEOUT_SECONDS = 30.0
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


class ProviderError(RuntimeError):
    """Provider 调用失败（中文短句，不含密钥）。"""


class OpenAICompatProvider:
    name = "openai-compat"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = TIMEOUT_SECONDS,
        opener=None,
    ):
        self.api_key = (api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")).strip()
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.getenv("MODEL") or DEFAULT_MODEL
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def submit(self, prompt: str, *, artifact_name: str, out_dir: str | Path) -> dict:
        """调一次模型，把结果写成产物文件；返回 task_id / artifact / usage。"""

        if not self.configured:
            raise ProviderError("未配置 OPENAI_API_KEY：无法调用真实 Provider")

        text = (prompt or "").strip()

        if not text:
            raise ProviderError("prompt 不能为空")

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是一个执行任务的助手，只输出任务要求的正文，不要解释。",
                },
                {"role": "user", "content": text},
            ],
            "max_tokens": MAX_TOKENS,
            "stream": False,
        }

        started = time.perf_counter()
        body = self._post("/chat/completions", payload)
        elapsed = round(time.perf_counter() - started, 2)

        content = self._content_of(body)

        if not content.strip():
            raise ProviderError("Provider 返回了空内容")

        target = Path(out_dir) / artifact_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        usage = body.get("usage") or {}
        task_id = str(body.get("id") or "")

        if not task_id:
            raise ProviderError("Provider 没有返回 id")

        return {
            "task_id": task_id,
            "provider": self.name,
            "model": self.model,
            "artifact": str(target),
            "duration_s": elapsed,
            "usage": {
                "prompt": int(usage.get("prompt_tokens") or 0),
                "completion": int(usage.get("completion_tokens") or 0),
                "total": int(usage.get("total_tokens") or 0),
            },
        }

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")

        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:200]
            raise ProviderError(f"Provider 返回 HTTP {error.code}：{detail}") from None

        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise ProviderError(f"连不上 Provider（{self.base_url}）：{reason}") from None

        try:
            return json.loads(raw)

        except json.JSONDecodeError:
            raise ProviderError("Provider 返回的不是 JSON") from None

    @staticmethod
    def _content_of(body: dict) -> str:
        choices = body.get("choices") or []

        if not choices:
            reason = json.dumps(body, ensure_ascii=False)[:200]
            raise ProviderError(f"Provider 没有返回 choices：{reason}")

        message = choices[0].get("message") or {}

        return str(message.get("content") or "")
