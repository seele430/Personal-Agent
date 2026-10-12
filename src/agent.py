"""Personal Agent 模型调用、提醒工具与 Token 用量记录。"""

import json
import logging
import os

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from dotenv import load_dotenv
from openai import OpenAI

from src.core.ai_usage import AIUsageStore
from src.core.reminder_identity import (
    ReminderDeliveryUnavailable,
    create_reminder_for_current_user as create_reminder,
)
from src.tools.reminder import init_db


# ==========================================
# 模型配置
# ==========================================

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    raise RuntimeError(
        "未找到 OPENAI_API_KEY，请检查 .env 文件"
    )

client = OpenAI(
    api_key=api_key,
    base_url=os.getenv(
        "OPENAI_BASE_URL",
        "https://api.deepseek.com",
    ),
    timeout=30.0,
    max_retries=0,
)

MODEL = os.getenv("MODEL", "deepseek-flash")

USAGE_DB_PATH = BASE_DIR / "ai_usage.db"

MAX_OUTPUT_TOKENS = 2048
MAX_USER_MESSAGE_BYTES = 32000

MAX_REMINDER_TOOL_CALLS = 1
MAX_REMINDER_MESSAGE_BYTES = 1024
MAX_TOOL_ARGUMENT_BYTES = 4096

logger = logging.getLogger(
    "personal_agent.usage"
)


# ==========================================
# 提醒工具定义
# ==========================================

REMINDER_TOOL = {
    "type": "function",
    "function": {
        "name": "create_reminder",
        "description": (
            "当用户明确要求几分钟后提醒某件事时，"
            "创建一条定时提醒。"
            "每次对话最多创建一条。"
            "如果提醒功能不可用，应明确告知用户。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "提醒内容",
                },
                "minutes": {
                    "type": "integer",
                    "description": "多少分钟后提醒",
                },
            },
            "required": [
                "message",
                "minutes",
            ],
        },
    },
}


# ==========================================
# 模型用量追踪
# ==========================================

@dataclass
class _UsageScope:
    user_id: str
    request_id: str
    completed_calls: int = 0


_current_usage: ContextVar[
    _UsageScope | None
] = ContextVar(
    "personal_agent_current_usage",
    default=None,
)


@contextmanager
def track_chat_usage(
    user_id: str,
    request_id: str,
) -> Iterator[_UsageScope]:

    if (
        not isinstance(user_id, str)
        or not user_id.strip()
    ):
        raise ValueError("无效的 user_id")

    if (
        not isinstance(request_id, str)
        or not request_id.strip()
    ):
        raise ValueError("无效的 request_id")

    scope = _UsageScope(
        user_id=user_id,
        request_id=request_id,
    )

    token = _current_usage.set(scope)

    try:
        yield scope
    finally:
        _current_usage.reset(token)


def _get_usage_store() -> AIUsageStore:
    return AIUsageStore(USAGE_DB_PATH)


# ==========================================
# 输入验证
# ==========================================

def validate_user_input(
    user_input: str,
) -> None:

    if not isinstance(user_input, str):
        raise ValueError("输入必须是文本")

    if not user_input.strip():
        raise ValueError("输入不能为空")

    if (
        len(user_input.encode("utf-8"))
        > MAX_USER_MESSAGE_BYTES
    ):
        raise ValueError(
            "输入内容太长，请缩短消息后重试"
        )


# ==========================================
# DeepSeek 专属参数
# ==========================================

def _is_deepseek_flash() -> bool:

    try:
        hostname = urlsplit(
            str(client.base_url)
        ).hostname

        return (
            hostname == "api.deepseek.com"
            and MODEL == "deepseek-flash"
        )

    except (TypeError, ValueError):
        return False


# ==========================================
# 执行一次模型调用
# ==========================================

def _completion(
    messages: list,
    tool_choice: str,
):

    kwargs = {
        "model": MODEL,
        "messages": messages,
        "tools": [REMINDER_TOOL],
        "tool_choice": tool_choice,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }

    if _is_deepseek_flash():
        kwargs["extra_body"] = {
            "thinking": {
                "type": "disabled",
            },
        }

    response = client.chat.completions.create(
        **kwargs
    )

    scope = _current_usage.get()

    if scope is not None:
        scope.completed_calls += 1

        try:
            _get_usage_store().record_completion(
                user_id=scope.user_id,
                request_id=scope.request_id,
                call_number=scope.completed_calls,
                model=MODEL,
                response=response,
            )

        except Exception:
            logger.exception(
                "模型用量保存失败 "
                "request_id=%s call_number=%s",
                scope.request_id,
                scope.completed_calls,
            )

    if not response.choices:
        raise RuntimeError(
            "模型没有返回有效结果"
        )

    finish_reason = getattr(
        response.choices[0],
        "finish_reason",
        None,
    )

    if finish_reason == "length":
        raise RuntimeError(
            "模型输出达到安全长度上限，"
            "回复可能不完整，请缩短问题"
        )

    if finish_reason not in (
        None,
        "stop",
        "tool_calls",
    ):
        raise RuntimeError(
            "模型未正常完成回复"
        )

    return response


# ==========================================
# 工具参数检查
# ==========================================

def _check_tool_call_arguments(
    tool_call,
) -> None:

    arguments = getattr(
        tool_call.function,
        "arguments",
        None,
    )

    if not isinstance(arguments, str):
        raise RuntimeError(
            "模型工具参数格式无效，没有创建提醒"
        )

    if (
        len(arguments.encode("utf-8"))
        > MAX_TOOL_ARGUMENT_BYTES
    ):
        raise RuntimeError(
            "模型工具参数过长，没有创建提醒"
        )


# ==========================================
# 统一处理提醒创建结果
# ==========================================

def _format_reminder_result(
    created,
) -> dict:
    """兼容正式身份模块和现有单元测试。

    正式 ReminderStore 返回 dict。
    旧测试可能模拟返回 (reminder_id, datetime)。

    兼容测试格式不意味着允许回退到旧创建接口。
    """

    if isinstance(created, dict):

        reminder_id = created["id"]
        remind_at = created["remind_at"]

        delivery = created.get("delivery")
        newly_created = created.get(
            "created",
            True,
        )

    elif (
        isinstance(created, tuple)
        and len(created) == 2
    ):

        reminder_id, remind_at = created

        delivery = None
        newly_created = True

    else:
        raise ValueError(
            "提醒服务返回了无效结果"
        )

    if isinstance(remind_at, datetime):
        remind_at_text = remind_at.isoformat()

    elif isinstance(remind_at, str):
        remind_at_text = remind_at

    else:
        raise ValueError(
            "提醒服务返回了无效时间"
        )

    result = {
        "success": True,
        "reminder_id": reminder_id,
        "remind_at": remind_at_text,
        "created": newly_created,
    }

    if delivery is not None:
        result["delivery"] = delivery

    return result


# ==========================================
# 主聊天函数
# ==========================================

def chat(user_input: str) -> str:

    validate_user_input(user_input)

    init_db()

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Personal Agent，中文私人助手。"
                f"当前本地时间："
                f"{datetime.now().astimezone().isoformat()}。"
                "用户要求若干分钟后提醒时，"
                "可以调用 create_reminder。"
                "缺少时间时先询问，不要猜测。"
                "工具返回失败时必须明确告知用户。"
                "只有工具返回成功，才能说提醒已保存。"
                "提醒已保存不代表通知已经送达。"
                "目前仅支持以整数分钟表示的"
                "相对时间提醒。"
            ),
        },
        {
            "role": "user",
            "content": user_input,
        },
    ]

    # 第一次模型调用
    response = _completion(
        messages,
        tool_choice="auto",
    )

    assistant_message = response.choices[0].message

    tool_calls = assistant_message.tool_calls

    if not tool_calls:
        return assistant_message.content or ""

    # 不允许一次执行多条提醒。
    if len(tool_calls) > MAX_REMINDER_TOOL_CALLS:
        raise RuntimeError(
            "模型一次请求创建多条提醒，"
            "已全部拒绝执行，请分开提出提醒要求"
        )

    # 先验证全部参数，再执行任何副作用。
    for tool_call in tool_calls:
        _check_tool_call_arguments(tool_call)

    messages.append(assistant_message)

    for tool_call in tool_calls:

        try:
            if (
                tool_call.function.name
                != "create_reminder"
            ):
                raise ValueError(
                    "未知工具"
                )

            args = json.loads(
                tool_call.function.arguments
            )

            if not isinstance(args, dict):
                raise ValueError(
                    "提醒参数格式无效"
                )

            message = args.get("message")
            minutes = args.get("minutes")

            if (
                not isinstance(message, str)
                or not message.strip()
            ):
                raise ValueError(
                    "提醒内容无效"
                )

            message = message.strip()

            if (
                len(message.encode("utf-8"))
                > MAX_REMINDER_MESSAGE_BYTES
            ):
                raise ValueError(
                    "提醒内容太长"
                )

            if (
                type(minutes) is not int
                or not 1 <= minutes <= 43200
            ):
                raise ValueError(
                    "提醒时间必须是 1～43200 分钟"
                )

            # 核心安全变化：
            # create_reminder 现在是经过身份验证的入口。
            # 没有绑定身份时绝不创建提醒。
            created = create_reminder(
                message,
                minutes,
            )

            result = _format_reminder_result(
                created
            )

        except (
            ValueError,
            TypeError,
            KeyError,
            PermissionError,
            ReminderDeliveryUnavailable,
        ) as exc:

            result = {
                "success": False,
                "error": str(exc),
            }

        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps(
                result,
                ensure_ascii=False,
            ),
        })

    # 最多一次补充模型调用
    final_response = _completion(
        messages,
        tool_choice="none",
    )

    return (
        final_response.choices[0].message.content
        or ""
    )