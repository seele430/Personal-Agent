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
    current_identity,
    list_reminders_for_current_user as list_reminders,
    update_reminder_for_current_user as update_reminder,
)
from src.knowledge.inject import (
    CITATION_RULE,
    InjectedContext,
    build_retrieval_context,
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

TOOL_RULES = (
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


def build_system_prompt(
    now: datetime,
    *,
    with_knowledge: bool = False,
    bot: dict | None = None,
) -> str:
    """聊天 system prompt。

    now 必须带时区。chat() 传入当前时间；
    eval 传入固定参考时间，保证两边提示词完全一致。

    with_knowledge=True 时追加引用规则：只有真的注入了检索片段
    才加，没有命中时不留"要求引用"的空指令。

    bot=None 时是默认个人助手；bot 传入时用身份卡（persona /
    system_prompt）覆盖人设，但保留工具调用能力（S3）。
    """

    if bot is None:
        prompt = (
            "你是 Personal Agent，中文私人助手。"
            f"当前本地时间：{now.isoformat()}。"
        )
    else:
        prompt = (
            f"你是「{bot['name']}」，一个 Bot。"
            f"你的天然立场（persona）：{bot['persona']}。"
            f"行为指令：{bot['system_prompt']}。"
            f"当前本地时间：{now.isoformat()}。"
        )

    prompt += TOOL_RULES

    if with_knowledge:
        prompt += CITATION_RULE

    return prompt


def utter(
    bot: dict,
    topic: str,
    *,
    word_limit: int = 80,
    subject: str | None = None,
) -> str:
    """让 Bot 就 topic 说一句核心观点（S4 发言）。

    subject = 房间议题；bot["stance"] = 这个 Bot 在本议题站的立场。
    两者都给了，Bot 才会「围绕议题、从阵营角度」说，而不是泛泛讲人设。

    与 chat 不同：纯生成一句话，不调工具、不注入知识库。
    限字数（word_limit），逼 Bot 只说最核心的判断。
    """

    if not isinstance(bot, dict) or not bot.get("name"):
        raise ValueError("bot 无效")

    if not isinstance(topic, str) or not topic.strip():
        raise ValueError("话题不能为空")

    if type(word_limit) is not int or word_limit < 10:
        raise ValueError("字数限制无效")

    topic = topic.strip()

    stance = str(bot.get("stance") or "").strip()
    subject = (subject or "").strip()

    context_lines = ""

    if subject:
        context_lines += f"你们正在讨论的议题：「{subject}」。\n"

    if stance:
        context_lines += f"你在本议题中站的立场：{stance}。\n"

    messages = [
        {
            "role": "system",
            "content": (
                f"你是「{bot['name']}」，一个 Bot。"
                f"你的天然立场（persona）：{bot['persona']}。"
                f"行为指令：{bot['system_prompt']}。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"{context_lines}"
                f"用户说：「{topic}」\n"
                f"请用不超过 {word_limit} 字回应，并主动给出你自己的判断。"
                f"要求：紧扣议题和用户这句话（不跑题、不只讲人设），"
                f"但必须亮明观点 —— "
                f"不要反问用户，不要把问题抛回去，"
                f"不要说「你想听哪方面」这类话，"
                f"也不要只复述立场而不给判断。"
            ),
        },
    ]

    # 中文 1 字 ≈ 1–2 token，留余量。
    kwargs = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": word_limit * 2 + 64,
    }

    if _is_deepseek_flash():
        kwargs["extra_body"] = {
            "thinking": {"type": "disabled"},
        }

    response = client.chat.completions.create(**kwargs)

    content = response.choices[0].message.content or ""

    return content.strip()


def host_summary(
    turn_messages: list[dict],
    topic: str,
) -> dict:
    """主持人汇总（S6）：讨论消息流 → 共识/分歧/决策选项。

    主持人独立角色，不参与讨论，只客观汇总并引导决策。
    """

    if not isinstance(turn_messages, list) or not turn_messages:
        raise ValueError("讨论记录不能为空")

    transcript = "\n".join(
        f"「{item.get('name', 'Bot')}」：{item['content']}"
        for item in turn_messages
    )

    prompt = (
        "你是主持人，不参与讨论，只负责客观汇总并引导决策。\n"
        f"话题：{topic}\n"
        f"讨论记录：\n{transcript}\n\n"
        "请输出一个 JSON 对象，字段如下：\n"
        "- consensus：多数人一致的共识（一句话）；\n"
        "- disagreement：立场冲突与分歧（一句话）；\n"
        "- options：2-3 个可执行选项，每个含 label（一句话）"
        "和 risk（代价/风险，一句话）。\n"
        "只输出 JSON，不要解释。"
    )

    kwargs = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
    }

    if _is_deepseek_flash():
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    response = client.chat.completions.create(**kwargs)

    raw = response.choices[0].message.content or "{}"

    try:
        return json.loads(raw)

    except json.JSONDecodeError:
        return {"consensus": "", "disagreement": "", "options": []}


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


def _knowledge_context(
    query: str,
    *,
    bot_id: str | None = None,
) -> InjectedContext | None:
    """按当前登录身份检索知识库；没有身份就不检索。

    没有认证上下文时**绝不**回退到某个默认用户 —— 那等于把别人的
    记忆喂进这次对话。检索失败也不影响聊天：只是这一轮不注入。

    bot_id=None 检索个人记忆；bot_id 非空检索该 Bot 的独立记忆（S3b）。
    """

    identity = current_identity()

    if identity is None:
        return None

    try:
        return build_retrieval_context(
            owner_id=identity.user_id,
            query=query,
            request_id=identity.request_id,
            bot_id=bot_id,
        )

    except Exception:
        logger.exception(
            "知识库检索失败，本轮不注入检索片段 "
            "user_id=%s",
            identity.user_id,
        )

        return None


def _record_citations(
    knowledge: InjectedContext | None,
    reply: str,
) -> None:
    """把回复里的 [n] 落回审计；失败不影响已经生成的回复。"""

    if knowledge is None or knowledge.empty:
        return

    try:
        knowledge.record_citations(reply)

    except Exception:
        logger.exception("知识库引用回写失败")


def chat(user_input: str, *, bot: dict | None = None) -> str:

    validate_user_input(user_input)

    init_db()

    # 检索只在有登录身份时发生（见 _knowledge_context）。
    # bot 私聊时按 bot_id 检索该 Bot 的独立记忆。
    bot_id = str(bot["id"]) if bot is not None else None

    knowledge = _knowledge_context(user_input, bot_id=bot_id)

    has_context = (
        knowledge is not None and not knowledge.empty
    )

    messages = [
        {
            "role": "system",
            "content": build_system_prompt(
                datetime.now().astimezone(),
                with_knowledge=has_context,
                bot=bot,
            ),
        },
    ]

    if has_context:
        # 片段放在 system 之后、用户消息之前：
        # 它是对模型可见的资料，不是用户说的话。
        messages.append({
            "role": "system",
            "content": knowledge.text,
        })

    messages.append({
        "role": "user",
        "content": user_input,
    })

    # 第一次模型调用
    response = _completion(
        messages,
        tool_choice="auto",
    )

    assistant_message = response.choices[0].message

    tool_calls = assistant_message.tool_calls

    if not tool_calls:
        reply = assistant_message.content or ""

        _record_citations(knowledge, reply)

        return reply

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

    reply = (
        final_response.choices[0].message.content
        or ""
    )

    _record_citations(knowledge, reply)

    return reply
