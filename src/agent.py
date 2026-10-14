"""Personal Agent 模型调用、提醒工具与 Token 用量记录。"""

import json
import logging
import os
import time
from src.config import paths

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
    cancel_reminder_for_current_user as cancel_reminder,
    create_reminder_for_current_user as create_reminder,
    list_reminders_for_current_user as list_reminders,
    update_reminder_for_current_user as update_reminder,
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

MAX_OUTPUT_TOKENS = 2048
MAX_USER_MESSAGE_BYTES = 32000

MAX_REMINDER_TOOL_CALLS = 1
MAX_REMINDER_MESSAGE_BYTES = 1024
MAX_TOOL_ARGUMENT_BYTES = 4096

# 单次列出的提醒条数上限：
# 不要把整张表塞进模型上下文。
MAX_LISTED_REMINDERS = 20

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
            "当用户要求提醒某件事且时间明确时，"
            "创建一条定时提醒。"
            "minutes 是距离当前本地时间的整数分钟。"
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


LIST_REMINDERS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_reminders",
        "description": (
            "列出当前用户自己的提醒，返回每条提醒的编号 id、"
            "内容、时间和状态。"
            "用户想知道自己有哪些提醒时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


CANCEL_REMINDER_TOOL = {
    "type": "function",
    "function": {
        "name": "cancel_reminder",
        "description": (
            "取消当前用户的一条尚未发送的提醒。"
            "reminder_id 必须来自 list_reminders 的结果，"
            "或用户明确说出的编号。"
            "没有编号时先询问或先列出，不要猜测。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reminder_id": {
                    "type": "integer",
                    "description": "提醒编号",
                },
            },
            "required": [
                "reminder_id",
            ],
        },
    },
}


UPDATE_REMINDER_TOOL = {
    "type": "function",
    "function": {
        "name": "update_reminder",
        "description": (
            "修改当前用户一条尚未发送的提醒的内容或时间。"
            "至少要给出 message 或 minutes 之一。"
            "reminder_id 必须来自 list_reminders 的结果，"
            "或用户明确说出的编号。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reminder_id": {
                    "type": "integer",
                    "description": "提醒编号",
                },
                "message": {
                    "type": "string",
                    "description": "新的提醒内容",
                },
                "minutes": {
                    "type": "integer",
                    "description": "从现在起多少分钟后提醒",
                },
            },
            "required": [
                "reminder_id",
            ],
        },
    },
}


# 暴露给模型的全部工具。
# 每个工具都必须经过登录身份校验才能读写数据。
REMINDER_TOOLS = [
    REMINDER_TOOL,
    LIST_REMINDERS_TOOL,
    CANCEL_REMINDER_TOOL,
    UPDATE_REMINDER_TOOL,
]


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
    return AIUsageStore(paths.ai_usage_db)


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
    tools: list | None = None,
):

    kwargs = {
        "model": MODEL,
        "messages": messages,
        "tools": REMINDER_TOOLS if tools is None else tools,
        "tool_choice": tool_choice,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }

    if _is_deepseek_flash():
        kwargs["extra_body"] = {
            "thinking": {
                "type": "disabled",
            },
        }

    # 单调时钟：不受系统时间调整影响
    started = time.monotonic()

    response = client.chat.completions.create(
        **kwargs
    )

    latency_ms = int(
        (time.monotonic() - started) * 1000
    )

    # trace 字段在校验之前提取，
    # 这样异常结束的调用也能留下记录。
    choice = (
        response.choices[0]
        if getattr(response, "choices", None)
        else None
    )

    finish_reason = getattr(
        choice,
        "finish_reason",
        None,
    )

    tool_names = [
        item.function.name
        for item in (
            getattr(
                getattr(choice, "message", None),
                "tool_calls",
                None,
            )
            or []
        )
        if getattr(
            getattr(item, "function", None),
            "name",
            None,
        )
    ]

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
                latency_ms=latency_ms,
                finish_reason=finish_reason,
                tool_names=tool_names,
            )

        except Exception:
            logger.exception(
                "模型用量保存失败 "
                "request_id=%s call_number=%s",
                scope.request_id,
                scope.completed_calls,
            )

    if choice is None:
        raise RuntimeError(
            "模型没有返回有效结果"
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

def build_system_prompt(now: datetime) -> str:
    """聊天 system prompt。

    now 必须带时区。chat() 传入当前时间；
    eval 传入固定参考时间，保证两边提示词完全一致。
    """

    return (
        "你是 Personal Agent，中文私人助手。"
        f"当前本地时间："
        f"{now.isoformat()}。"
        "用户要求提醒且时间明确时，可以调用 create_reminder。"
        "用户想知道自己有哪些提醒时，调用 list_reminders。"
        "取消或修改提醒需要提醒编号："
        "先用 list_reminders 取得编号，或使用用户明确说出的编号；"
        "没有编号时先询问，绝不猜测编号。"
        "取消和修改只对尚未发送的提醒有效。"
        "用户给出具体钟点（如今晚8点、明天9点）时，"
        "按当前本地时间换算成整数分钟后调用，"
        "并在回复中说明换算后的提醒时间。"
        "只有日期或时段、没有具体钟点（如下周一早上），"
        "或时间已经过去时，先询问，不要猜测。"
        "缺少时间时先询问，不要猜测。"
        "工具返回失败时必须明确告知用户。"
        "只有工具返回成功，才能说提醒已保存。"
        "提醒已保存不代表通知已经送达。"
    )


# ==========================================
# 执行一次提醒工具调用
# ==========================================

def _tool_arguments(tool_call) -> dict:
    """解析工具参数，必须是一个 JSON 对象。"""

    args = json.loads(
        tool_call.function.arguments
    )

    if not isinstance(args, dict):
        raise ValueError("工具参数格式无效")

    return args


def _require_reminder_id(args: dict) -> int:
    reminder_id = args.get("reminder_id")

    if type(reminder_id) is not int or reminder_id <= 0:
        raise ValueError("提醒编号无效")

    return reminder_id


def _clean_reminder_message(message) -> str:
    if (
        not isinstance(message, str)
        or not message.strip()
    ):
        raise ValueError("提醒内容无效")

    message = message.strip()

    if (
        len(message.encode("utf-8"))
        > MAX_REMINDER_MESSAGE_BYTES
    ):
        raise ValueError("提醒内容太长")

    return message


def _clean_reminder_minutes(minutes) -> int:
    if (
        type(minutes) is not int
        or not 1 <= minutes <= 43200
    ):
        raise ValueError(
            "提醒时间必须是 1～43200 分钟"
        )

    return minutes


def _execute_reminder_tool(tool_call) -> dict:
    """执行一次提醒工具调用。

    每个分支都通过经过认证的入口读写数据：
    没有登录身份时一律拒绝，
    也不会回退到旧的、不区分用户的全站创建接口。
    """

    name = tool_call.function.name

    if name == "list_reminders":
        reminders = list_reminders()

        return {
            "success": True,
            "count": len(reminders),
            # 只返回渲染所需字段，并限制条数。
            "reminders": [
                {
                    "id": item["id"],
                    "message": item["message"],
                    "remind_at": item["remind_at"],
                    "status": item["status"],
                }
                for item in
                reminders[:MAX_LISTED_REMINDERS]
            ],
        }

    if name == "cancel_reminder":
        args = _tool_arguments(tool_call)

        cancelled = cancel_reminder(
            _require_reminder_id(args)
        )

        return {
            "success": True,
            "reminder_id": cancelled["id"],
            "status": cancelled["status"],
            "changed": cancelled["changed"],
        }

    if name == "update_reminder":
        args = _tool_arguments(tool_call)

        reminder_id = _require_reminder_id(args)

        message = args.get("message")
        minutes = args.get("minutes")

        if message is None and minutes is None:
            raise ValueError(
                "请给出新的提醒内容或新的提醒时间"
            )

        updated = update_reminder(
            reminder_id,
            message=(
                None
                if message is None
                else _clean_reminder_message(message)
            ),
            minutes=(
                None
                if minutes is None
                else _clean_reminder_minutes(minutes)
            ),
        )

        return {
            "success": True,
            "reminder_id": updated["id"],
            "message": updated["message"],
            "minutes": updated["minutes"],
            "remind_at": updated["remind_at"],
            "changed": updated["changed"],
        }

    if name != "create_reminder":
        raise ValueError("未知工具")

    args = _tool_arguments(tool_call)

    # 核心安全变化：
    # create_reminder 现在是经过身份验证的入口。
    # 没有绑定身份时绝不创建提醒。
    created = create_reminder(
        _clean_reminder_message(args.get("message")),
        _clean_reminder_minutes(args.get("minutes")),
    )

    return _format_reminder_result(created)


def chat(user_input: str) -> str:

    validate_user_input(user_input)

    init_db()

    messages = [
        {
            "role": "system",
            "content": build_system_prompt(
                datetime.now().astimezone()
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
            result = _execute_reminder_tool(
                tool_call
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