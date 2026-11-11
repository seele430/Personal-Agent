"""聊天记忆工具：memory_remember（ask）与 memory_recall（allow）。"""

from __future__ import annotations

import json

from src.core.reminder_identity import current_identity
from src.memory import service
from src.memory.models import MemoryError


REMEMBER_TOOL = {
    "type": "function",
    "function": {
        "name": "memory_remember",
        "description": "仅在用户明确要求记住某件事时写入长期记忆；不要自动保存所有对话。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "text": {"type": "string", "description": "要记住的事实，1–4000 字符"},
                "key": {"type": "string", "description": "可选：同一主题的稳定键，便于更新"},
                "kind": {
                    "type": "string",
                    "enum": ["preference", "project_fact", "constraint", "episode"],
                },
                "layer": {"type": "string", "enum": ["identity", "project", "session"]},
            },
            "required": ["text"],
        },
    },
}

RECALL_TOOL = {
    "type": "function",
    "function": {
        "name": "memory_recall",
        "description": "按关键词检索当前用户的长期记忆。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "description": "检索关键词，1–500 字符"},
                "k": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        },
    },
}


def _arguments(tool_call, allowed: set[str]) -> dict:
    args = json.loads(tool_call.function.arguments)

    if not isinstance(args, dict):
        raise ValueError("工具参数格式无效")

    if set(args) - allowed:
        raise ValueError("工具参数含不允许的字段")

    return args


def _identity():
    identity = current_identity()

    if identity is None:
        raise PermissionError("没有经过认证的身份，拒绝访问记忆")

    return identity


def remember_tool(tool_call, cancel_token=None) -> dict:
    args = _arguments(tool_call, {"text", "key", "kind", "layer"})
    identity = _identity()
    text = args.get("text")

    if not isinstance(text, str) or not text.strip():
        raise ValueError("text 不能为空")

    item = service.remember(
        identity.user_id,
        text,
        key=str(args.get("key") or ""),
        kind=str(args.get("kind") or "project_fact"),
        layer=str(args.get("layer") or "project"),
        trust="user_confirmed",
        source_ref=f"chat:{identity.request_id}",
    )

    return {
        "id": item.id,
        "key": item.key,
        "supersedes_id": item.supersedes_id,
        "status": "remembered",
    }


def recall_tool(tool_call, cancel_token=None) -> dict:
    args = _arguments(tool_call, {"query", "k"})
    identity = _identity()
    query = args.get("query")
    k = args.get("k", 5)

    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 10:
        raise ValueError("k 必须是 1..10")

    try:
        result = service.recall(identity.user_id, str(query or ""), k=k)
    except MemoryError as error:
        raise ValueError(str(error)) from None

    return {
        "mode": result["mode"],
        "items": [
            {
                "id": hit.item.id,
                "key": hit.item.key,
                "text": hit.item.text,
                "trust": hit.item.trust,
                "rank": hit.rank,
                "score": hit.score,
            }
            for hit in result["hits"]
        ],
    }


MODEL_VISIBLE_TOOLS = [REMEMBER_TOOL, RECALL_TOOL]
TOOL_DEFINITIONS = [
    (REMEMBER_TOOL, "ask", remember_tool),
    (RECALL_TOOL, "allow", recall_tool),
]
