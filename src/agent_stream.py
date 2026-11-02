"""单用户对话的流式 run 编排、取消事件与 run / step / exec 留痕。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from src import agent
from src.core.run_control import CancellationToken, ChatCancelled
from src.core.run_store import RunStore
from src.core.tool_registry import ToolApprovalRequired, ToolDenied


EventEmitter = Callable[[dict[str, Any]], None]


def _assistant_message_dict(message) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": message.content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in message.tool_calls or []
        ],
    }


def _initial_messages(user_input: str, bot: dict | None):
    agent.validate_user_input(user_input)
    agent.init_db()
    bot_id = str(bot["id"]) if bot is not None else None
    knowledge = agent._knowledge_context(user_input, bot_id=bot_id)
    has_context = knowledge is not None and not knowledge.empty
    messages = [
        {
            "role": "system",
            "content": agent.build_system_prompt(
                datetime.now().astimezone(),
                with_knowledge=has_context,
                bot=bot,
            ),
        }
    ]
    if has_context:
        messages.append({"role": "system", "content": knowledge.text})
    messages.append({"role": "user", "content": user_input})
    return messages, knowledge


def run_chat_stream(
    user_input: str,
    *,
    emit: EventEmitter,
    cancel_token: CancellationToken,
    bot: dict | None = None,
) -> str:
    """Run at most two streaming model calls and one approved tool call."""

    identity = agent.current_identity()
    store = RunStore() if identity is not None else None
    run = turn = None
    if store is not None:
        run = store.create_run(
            owner_id=identity.user_id, request_id=identity.request_id
        )
        turn = store.create_turn(run["id"], user_message=user_input)
        store.add_step(
            turn["id"], kind="user", actor="user",
            content=user_input, status="succeeded",
        )
    run_id = run["id"] if run is not None else None
    active_step = None
    active_parts: list[str] = []
    terminal_emitted = False

    def start_assistant_step():
        nonlocal active_step, active_parts
        active_parts = []
        active_step = (
            store.add_step(
                turn["id"], kind="assistant", actor="assistant", content=""
            )
            if store is not None else None
        )

    def on_delta(text: str) -> None:
        cancel_token.check()
        active_parts.append(text)
        emit({"type": "text_delta", "run_id": run_id, "text": text})

    def finish_assistant_step(content: str) -> None:
        nonlocal active_step
        if store is not None and active_step is not None:
            store.update_step(active_step["id"], status="succeeded", content=content)
        active_step = None

    try:
        emit({"type": "run_started", "run_id": run_id})
        messages, knowledge = _initial_messages(user_input, bot)
        start_assistant_step()
        response = agent._completion(
            messages, tool_choice="auto",
            on_delta=on_delta, cancel_token=cancel_token,
        )
        assistant = response.choices[0].message
        finish_assistant_step(assistant.content or "")
        tool_calls = assistant.tool_calls

        if not tool_calls:
            reply = assistant.content or ""
            agent._record_citations(knowledge, reply)
            if store is not None:
                store.finish(run_id, status="succeeded")
            terminal_emitted = True
            emit({"type": "done", "run_id": run_id, "reply": reply})
            return reply

        if len(tool_calls) > agent.MAX_REMINDER_TOOL_CALLS:
            raise RuntimeError("模型一次请求多个工具，已全部拒绝执行")
        for tool_call in tool_calls:
            agent._check_tool_call_arguments(tool_call)
        messages.append(_assistant_message_dict(assistant))

        for tool_call in tool_calls:
            cancel_token.check()
            name = tool_call.function.name
            policy = agent.TOOL_REGISTRY.policy(name)
            tool_step = (
                store.add_step(
                    turn["id"], kind="tool", actor="assistant", content=name
                )
                if store is not None else None
            )
            execution = None
            if policy == "allow" and store is not None:
                execution = store.record_exec(
                    tool_step["id"], tool_name=name,
                    request=agent._tool_arguments(tool_call), status="running",
                )
            emit({
                "type": "tool_started", "run_id": run_id,
                "tool": name, "policy": policy,
                "exec_id": execution["id"] if execution is not None else None,
            })
            try:
                result = agent.TOOL_REGISTRY.execute(
                    tool_call, cancel_token=cancel_token
                )
            except ChatCancelled:
                if store is not None and execution is not None:
                    store.update_exec(execution["id"], status="cancelled")
                    store.update_step(tool_step["id"], status="cancelled", content=name)
                raise
            except (
                ValueError, TypeError, KeyError, PermissionError,
                agent.ReminderDeliveryUnavailable,
            ) as exc:
                result = {"success": False, "error": str(exc)}
                if store is not None:
                    if execution is not None:
                        store.update_exec(
                            execution["id"], status="failed", result=result,
                            error_code="tool_failed",
                        )
                    status = (
                        "awaiting_approval"
                        if isinstance(exc, ToolApprovalRequired)
                        else "denied" if isinstance(exc, ToolDenied)
                        else "failed"
                    )
                    store.update_step(
                        tool_step["id"], status=status,
                        content=json.dumps(result, ensure_ascii=False),
                    )
                if isinstance(exc, ToolApprovalRequired):
                    emit({"type": "approval_required", "run_id": run_id, "tool": name})
                elif isinstance(exc, ToolDenied):
                    emit({"type": "tool_denied", "run_id": run_id, "tool": name})
            else:
                if store is not None and execution is not None:
                    store.update_exec(
                        execution["id"], status="succeeded", result=result
                    )
                    store.update_step(
                        tool_step["id"], status="succeeded",
                        content=json.dumps(result, ensure_ascii=False),
                    )
            emit({
                "type": "tool_finished", "run_id": run_id,
                "tool": name, "result": result,
                "exec_id": execution["id"] if execution is not None else None,
            })
            messages.append({
                "role": "tool", "tool_call_id": tool_call.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

        start_assistant_step()
        final_response = agent._completion(
            messages, tool_choice="none",
            on_delta=on_delta, cancel_token=cancel_token,
        )
        reply = final_response.choices[0].message.content or ""
        finish_assistant_step(reply)
        agent._record_citations(knowledge, reply)
        if store is not None:
            store.finish(run_id, status="succeeded")
        terminal_emitted = True
        emit({"type": "done", "run_id": run_id, "reply": reply})
        return reply
    except ChatCancelled:
        if store is not None:
            if active_step is not None:
                store.update_step(
                    active_step["id"], status="cancelled",
                    content="".join(active_parts),
                )
            store.finish(run_id, status="cancelled")
        terminal_emitted = True
        emit({"type": "cancelled", "run_id": run_id})
        raise
    except Exception as exc:
        if store is not None:
            if active_step is not None:
                store.update_step(
                    active_step["id"], status="failed",
                    content="".join(active_parts),
                )
            store.finish(run_id, status="failed")
        if not terminal_emitted:
            emit({"type": "run_failed", "run_id": run_id, "error": str(exc)})
        raise
