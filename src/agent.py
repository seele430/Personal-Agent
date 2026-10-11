import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent

# 读取项目根目录中的 .env
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
        "https://api.deepseek.com"
    ),
    timeout=30.0,
    max_retries=2,
)

MODEL = os.getenv("MODEL", "deepseek-flash")


import json
from datetime import datetime

from src.tools.reminder import init_db, create_reminder

REMINDER_TOOL = {
    "type": "function",
    "function": {
        "name": "create_reminder",
        "description": (
            "当用户明确要求几分钟后提醒某件事时，"
            "创建一个真实的定时提醒。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "需要提醒用户做什么"
                },
                "minutes": {
                    "type": "integer",
                    "description": "从现在起多少分钟后提醒"
                }
            },
            "required": ["message", "minutes"]
        }
    }
}


def chat(user_input: str) -> str:
    if not user_input.strip():
        raise ValueError("输入不能为空")

    init_db()

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Personal Agent，中文私人助手。"
                f"当前本地时间：{datetime.now().astimezone().isoformat()}。"
                "用户要求若干分钟后提醒时，调用 create_reminder。"
                "缺少时间时先询问，不要猜测。"
                "不能声称未实际创建的提醒已经创建。"
                "目前仅支持以整数分钟表示的相对时间提醒。"
            )
        },
        {
            "role": "user",
            "content": user_input
        }
    ]

    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=[REMINDER_TOOL],
        tool_choice="auto"
    )

    assistant_message = response.choices[0].message

    if not assistant_message.tool_calls:
        return assistant_message.content or ""

    messages.append(assistant_message)

    for tool_call in assistant_message.tool_calls:
        try:
            if tool_call.function.name != "create_reminder":
                raise ValueError("未知工具")

            args = json.loads(tool_call.function.arguments)
            message = args.get("message")
            minutes = args.get("minutes")

            if not isinstance(message, str) or not message.strip():
                raise ValueError("提醒内容无效")

            if type(minutes) is not int or not 1 <= minutes <= 43200:
                raise ValueError("提醒时间必须是 1～43200 分钟")

            reminder_id, remind_at = create_reminder(
                message.strip(),
                minutes
            )

            result = {
                "success": True,
                "reminder_id": reminder_id,
                "remind_at": remind_at.isoformat()
            }

        except (ValueError, TypeError, KeyError) as exc:
            result = {
                "success": False,
                "error": str(exc)
            }

        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps(result, ensure_ascii=False)
        })

    final_response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=[REMINDER_TOOL],
        tool_choice="none"
    )

    return final_response.choices[0].message.content or ""