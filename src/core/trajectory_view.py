"""D14：把一次 run 的 step / exec / 审批 / trajectory 事件渲染成可读时间线。

只读模块：不写数据库、不改状态。命令行入口见 ``scripts/trace_run.py``。
完整事件定义见 ``docs/trajectory-schema.md``。
"""

from __future__ import annotations

import json

from typing import Any

TEXT_LIMIT = 120
PAYLOAD_LIMIT = 120


def short_text(value: Any, limit: int = TEXT_LIMIT) -> str:
    if value is None:
        return ""

    text = " ".join(str(value).split())

    if len(text) <= limit:
        return text

    return text[:limit] + "…"


def _payload_text(event: dict[str, Any]) -> str:
    payload = event.get("payload")

    if not payload:
        return ""

    # D24：exec_started 只带一个 traceparent，缩成「trace… span…」更好扫。
    traceparent = payload.get("traceparent")

    if isinstance(traceparent, str) and len(payload) == 1:
        parts = traceparent.split("-")

        if len(parts) == 4:
            return f"trace={parts[1][:8]}… span={parts[2][:8]}"

    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    return short_text(text, PAYLOAD_LIMIT)


def collect_run(
    store,
    run_id: str,
    *,
    owner_id: str | None = None,
) -> dict[str, Any]:
    """一次读齐：run（含 turn / step / exec）、事件、审批。"""

    return {
        "run": store.get_run(run_id, owner_id=owner_id),
        "events": store.list_events(run_id),
        "approvals": store.list_approvals(run_id),
    }


def render(data: dict[str, Any]) -> str:
    """渲染成文本时间线；行内事件按 run 内 sequence 升序。"""

    run = data["run"]
    events = data["events"]

    lines = [
        f"run {run['id']}  status={run['status']}  owner={run['owner_id']}",
        f"created={run['created_at']}  updated={run['updated_at']}",
        "",
    ]

    run_level = [event for event in events if event["step_id"] is None]
    by_step: dict[str, list[dict[str, Any]]] = {}

    for event in events:
        if event["step_id"] is not None:
            by_step.setdefault(event["step_id"], []).append(event)

    if run_level:
        lines.append("run 事件：")

        for event in run_level:
            detail = _payload_text(event)
            suffix = f"  {detail}" if detail else ""
            lines.append(
                f"  · {event['sequence']:>3} "
                f"{event['event_type']}/{event['status']}{suffix}"
            )

        lines.append("")

    for turn in run.get("turns", []):
        lines.append(
            f"turn {turn['sequence']}  {turn['status']}  "
            f"{short_text(turn['user_message'])}"
        )

        for step in turn["steps"]:
            execution = step.get("exec")
            exec_text = ""

            if execution:
                exec_text = (
                    f"  {execution['tool_name']} "
                    f"exec={execution['id']} [{execution['status']}]"
                )

            lines.append(
                f"  step {step['sequence']:<2} {step['kind']:<9} "
                f"{step['status']:<10}{exec_text}"
            )

            content = short_text(step.get("content"))

            if content:
                lines.append(f"       {content}")

            for event in by_step.get(step["id"], []):
                detail = _payload_text(event)
                suffix = f"  {detail}" if detail else ""
                lines.append(
                    f"       · {event['sequence']:>3} "
                    f"{event['event_type']}/{event['status']}{suffix}"
                )

        lines.append("")

    approvals = data.get("approvals") or []

    if approvals:
        lines.append("审批：")

        for approval in approvals:
            lines.append(
                f"  {approval['id']}  {approval['tool_name']}  "
                f"{approval['status']}  "
                f"by={approval.get('decided_by') or '-'}  "
                f"{short_text(approval['arguments_preview'], 80)}"
            )

    return "\n".join(line.rstrip() for line in lines).rstrip() + "\n"
