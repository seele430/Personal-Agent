"""研究侧的模型调用层：非流式、有界、错误分类。

为什么不复用 `src.agent._completion`：那是聊天路径的私有函数，默认带
提醒工具表、流式回调与聊天用量口径；`import src.agent` 实测要 **2.72 s**
（会顺带拉起工具表、各 store 与调度器）。研究阶段要的是"一次纯文本补全
+ 明确的超时/重试/用量"，所以这里自建一个薄客户端，配置项与
`src/agent.py` 保持一致（`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL`）。

三条纪律：

1. **非流式**：研究阶段要完整 JSON，流式只会增加解析分支。
2. **有界**：`max_tokens` / `timeout` 都有硬上限；重试只针对 429/5xx/超时，且最多 1 次。
3. **失败显式**：`LLMError` 的四个子类对应四种处置，调用方不许"失败当空串"。

传输可注入（`completer=`），所以单测完全离线。
"""

from __future__ import annotations

import json
import os
import re
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[2]

# 与 src/agent.py 同一条规则：tier1 沙箱子进程绝不读宿主 .env
if os.getenv("PERSONAL_AGENT_TIER1") != "1":
    load_dotenv(BASE_DIR / ".env")


DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"

MAX_OUTPUT_TOKENS = 4096
MIN_OUTPUT_TOKENS = 64
MAX_TIMEOUT = 180.0
MAX_RETRIES = 1

MAX_MESSAGES = 24
MAX_MESSAGE_CHARS = 60_000

ALLOWED_ROLES = frozenset({"system", "user", "assistant"})

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

_FENCE = re.compile(
    r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)


class LLMError(RuntimeError):
    """模型调用失败的基类。"""


class LLMAuthError(LLMError):
    """凭据缺失或被拒 —— 重试没有意义。"""


class LLMRateLimited(LLMError):
    """被限流（429）。"""


class LLMTimeout(LLMError):
    """超时。"""


class LLMUnavailable(LLMError):
    """连接失败或 5xx。"""


class LLMInvalidResponse(LLMError):
    """返回内容不是预期的 JSON。"""


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class LLMCall:
    """一次成功调用的结果 + 用量（成本账本只记用量，不编单价）。"""

    text: str
    usage: LLMUsage
    latency_ms: float
    attempts: int
    model: str = ""


#: 注入点：completer(messages, params) -> 响应对象（需有 choices/usage）
Completer = Callable[[list[dict], dict], object]


def model_name() -> str:
    return os.getenv("MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _is_flash_on_deepseek() -> bool:
    """与 `src/agent.py` 同一条判断：flash 在官方端点要关掉 thinking。

    不关的话，思考 token 会吃掉 max_tokens，表现为**输出被截断成半截
    JSON** —— 2026-11-07 真实运行就是这么失败的。
    """

    from urllib.parse import urlsplit

    try:
        hostname = urlsplit(
            os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL)
        ).hostname

    except (TypeError, ValueError):
        return False

    return hostname == "api.deepseek.com" and model_name() == "deepseek-flash"


def _extra_body() -> dict | None:
    if _is_flash_on_deepseek():
        return {"thinking": {"type": "disabled"}}

    return None


def _build_client():
    from openai import OpenAI

    return OpenAI(
        api_key=os.getenv("OPENAI_API_KEY") or "missing-api-key",
        base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL),
        timeout=60.0,
        # 重试由本模块统一控制，SDK 自己再重试会让"最多 1 次"失去意义
        max_retries=0,
    )


_CLIENT = None


def _default_completer(messages: list[dict], params: dict):
    global _CLIENT

    if _CLIENT is None:
        _CLIENT = _build_client()

    return _CLIENT.chat.completions.create(messages=messages, **params)


def _validate_messages(messages) -> list[dict]:
    if not isinstance(messages, (list, tuple)) or not messages:
        raise ValueError("messages 不能为空")

    if len(messages) > MAX_MESSAGES:
        raise ValueError(f"messages 超过 {MAX_MESSAGES} 条")

    cleaned = []

    for item in messages:
        if not isinstance(item, dict):
            raise ValueError("messages 的每一项必须是 dict")

        role = item.get("role")
        content = item.get("content")

        if role not in ALLOWED_ROLES:
            raise ValueError(f"不支持的 role：{role!r}")

        if not isinstance(content, str) or not content.strip():
            raise ValueError("message.content 不能为空")

        if len(content) > MAX_MESSAGE_CHARS:
            raise ValueError("单条 message 过长")

        cleaned.append({"role": role, "content": content})

    return cleaned


def _validate_params(max_tokens, temperature, timeout) -> None:
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or not MIN_OUTPUT_TOKENS <= max_tokens <= MAX_OUTPUT_TOKENS
    ):
        raise ValueError(
            f"max_tokens 必须在 {MIN_OUTPUT_TOKENS}..{MAX_OUTPUT_TOKENS}"
        )

    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not 0.0 <= float(temperature) <= 1.5
    ):
        raise ValueError("temperature 必须在 0.0..1.5")

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1.0 <= float(timeout) <= MAX_TIMEOUT
    ):
        raise ValueError(f"timeout 必须在 1..{MAX_TIMEOUT} 秒")


def _extract_text(response) -> str:
    try:
        choices = response.choices

    except AttributeError as error:
        raise LLMInvalidResponse("响应缺少 choices") from error

    if not choices:
        raise LLMInvalidResponse("响应没有任何 choice")

    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)

    if not isinstance(content, str):
        raise LLMInvalidResponse("响应缺少文本内容")

    return content


def _extract_usage(response) -> LLMUsage:
    usage = getattr(response, "usage", None)

    if usage is None:
        return LLMUsage()

    return LLMUsage(
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
    )


def _classify(error: Exception) -> LLMError:
    """把 SDK 异常翻译成本模块的四个子类。"""

    status = getattr(error, "status_code", None)

    if status in (401, 403):
        return LLMAuthError(f"模型凭据被拒绝（HTTP {status}）")

    if status == 429:
        return LLMRateLimited("模型限流（HTTP 429）")

    if status in RETRYABLE_STATUSES:
        return LLMUnavailable(f"模型服务端错误（HTTP {status}）")

    name = type(error).__name__

    if "Timeout" in name:
        return LLMTimeout(f"模型调用超时：{error}")

    if status is not None:
        return LLMAuthError(f"模型调用被拒（HTTP {status}）")

    if "Connection" in name:
        return LLMUnavailable(f"模型连接失败：{error}")

    return LLMUnavailable(f"模型调用失败：{error}")


def _is_retryable(error: LLMError) -> bool:
    return isinstance(error, (LLMRateLimited, LLMTimeout, LLMUnavailable))


def complete(
    messages,
    *,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    temperature: float = 0.0,
    timeout: float = 120.0,
    max_retries: int = MAX_RETRIES,
    retry_delay: float = 1.0,
    completer: Completer | None = None,
    sleep=time.sleep,
) -> LLMCall:
    """跑一次非流式补全，返回文本与用量。

    `completer` / `sleep` 可注入，测试完全离线且不用真的等待。
    """

    cleaned = _validate_messages(messages)
    _validate_params(max_tokens, temperature, timeout)

    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        raise ValueError("max_retries 必须是整数")

    if not 0 <= max_retries <= MAX_RETRIES:
        raise ValueError(f"max_retries 必须在 0..{MAX_RETRIES}")

    params = {
        "model": model_name(),
        "max_tokens": max_tokens,
        "temperature": float(temperature),
        "timeout": float(timeout),
    }

    extra = _extra_body()

    if extra is not None:
        params["extra_body"] = extra

    runner = completer or _default_completer
    attempts = 0
    last_error: LLMError | None = None

    while attempts <= max_retries:
        attempts += 1
        started = time.perf_counter()

        try:
            response = runner(cleaned, params)

        except Exception as error:  # noqa: BLE001 - 统一翻译
            last_error = _classify(error)

            if _is_retryable(last_error) and attempts <= max_retries:
                if retry_delay:
                    sleep(retry_delay)

                continue

            raise last_error from error

        latency_ms = round((time.perf_counter() - started) * 1000, 1)

        return LLMCall(
            text=_extract_text(response),
            usage=_extract_usage(response),
            latency_ms=latency_ms,
            attempts=attempts,
            model=params["model"],
        )

    raise last_error or LLMUnavailable("模型调用失败")


def parse_json_object(text: str) -> dict:
    """从模型输出里取出 JSON 对象。

    容忍 ```` ```json ```` 围栏；其余情况一律当"响应不合法"，
    **不做"猜它想写什么"的修补**。
    """

    if not isinstance(text, str) or not text.strip():
        raise LLMInvalidResponse("模型输出为空")

    body = text.strip()
    fence = _FENCE.match(body)

    if fence is not None:
        body = fence.group("body").strip()

    try:
        payload = json.loads(body)

    except json.JSONDecodeError as error:
        raise LLMInvalidResponse(
            f"模型输出不是合法 JSON：{body[:120]!r}"
        ) from error

    if not isinstance(payload, dict):
        raise LLMInvalidResponse("模型输出的 JSON 顶层不是对象")

    return payload


def complete_json(
    messages,
    *,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    temperature: float = 0.0,
    timeout: float = 120.0,
    max_retries: int = MAX_RETRIES,
    retry_delay: float = 1.0,
    completer: Completer | None = None,
    sleep=time.sleep,
) -> tuple[dict, LLMCall]:
    """`complete` + JSON 解析：返回值与用量一起给调用方。"""

    call = complete(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        max_retries=max_retries,
        retry_delay=retry_delay,
        completer=completer,
        sleep=sleep,
    )

    return parse_json_object(call.text), call


def reset_client() -> None:
    """测试用：丢掉缓存的 client。"""

    global _CLIENT
    _CLIENT = None
