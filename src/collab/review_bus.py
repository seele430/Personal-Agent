"""M1 + M2 文件总线：复审结果校验/归档/状态推进，修复任务与下一轮草稿，确认发送、通知、停止。

边界（对齐 M2 计划 v1.1）：

- 只读 ``kimi/outbox``，只写 ``pa/outbox``、``state``、``archive`` 与 ``kimi/inbox``（仅发送时）；
- 草稿落 ``pa/outbox/drafts``，**任何自动流程都不写 ``kimi/inbox``**；
- 发送 = 人工确认后一次 ``os.replace``，同时推进 ``state.round``（否则 M1 的 round 校验会拒收结果）；
- 3 轮上限按「同一复审对象」计（``loop_round``），全局 ``round`` 只作文件序号；
- 不自动改代码、不自动发送、不发模型请求。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


DEFAULT_ROOT = Path(r"C:\Users\123\PA-Shared")
MAX_RESULT_BYTES = 2 * 1024 * 1024
MIN_DRAFT_BYTES = 200
MAX_DRAFT_BYTES = 200 * 1024
MAX_ROUNDS = 3
DEFAULT_TIMEOUT = 600.0
DRAFT_SECTION = "## 请复审"

FILENAME = re.compile(r"^round-(\d{3})-result\.json$")
REQUEST_FILENAME = re.compile(r"^round-(\d{3})-request\.md$")
VERDICTS = ("pass", "findings", "blocked")
SEVERITIES = ("blocker", "high", "medium", "low")

Notifier = Callable[[str], None]


# ---------------------------------------------------------------- 基础工具

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def _state_path(root: Path) -> Path:
    return Path(root) / "state" / "review.json"


def load_state(root: Path = DEFAULT_ROOT) -> dict:
    return json.loads(_state_path(root).read_text(encoding="utf-8"))


def save_state_atomic(root: Path, state: dict) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=".review-", suffix=".tmp", dir=str(path.parent)
    )

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write_text_atomic(path: Path, text: str, *, prefix: str = ".draft-") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=prefix, suffix=".tmp", dir=str(path.parent)
    )

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _reject_symlink(path: Path) -> bool:
    return path.is_symlink() or bool(
        getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400
    )


def _ensure_state_defaults(state: dict) -> None:
    state.setdefault("target", None)
    state.setdefault("loop_round", 0)
    state.setdefault("request_sent_at", None)
    state.setdefault("draft_file", None)
    state.setdefault("fix_task_file", None)
    state.setdefault("notified", {})


def _history(state: dict, round_number: int, event: str, **fields) -> None:
    entry = {"round": round_number, "event": event, "by": "pa", "at": _now()}
    entry.update(fields)
    state.setdefault("history", []).append(entry)


def _to_timestamp(value) -> float | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.timestamp()


# ---------------------------------------------------------------- 路径

def drafts_dir(root: Path) -> Path:
    return Path(root) / "pa" / "outbox" / "drafts"


def draft_path(root: Path, round_number: int) -> Path:
    return drafts_dir(root) / f"round-{round_number:03d}-request.md"


def inbox_request_path(root: Path, round_number: int) -> Path:
    return Path(root) / "kimi" / "inbox" / f"round-{round_number:03d}-request.md"


def fix_task_path(root: Path, round_number: int) -> Path:
    return Path(root) / "pa" / "outbox" / f"round-{round_number:03d}-fix-task.md"


# ---------------------------------------------------------------- 结果校验

def validate_result(payload: dict, state: dict) -> list[str]:
    errors: list[str] = []

    if not isinstance(payload, dict):
        return ["not_object"]

    if payload.get("round") != state.get("round"):
        errors.append("round_mismatch")

    verdict = payload.get("verdict")

    if verdict not in VERDICTS:
        errors.append("bad_verdict")

    findings = payload.get("findings")

    if not isinstance(findings, list):
        errors.append("findings_not_list")
        findings = []

    if verdict == "pass" and findings:
        errors.append("pass_with_findings")

    for item in findings:
        if not isinstance(item, dict):
            errors.append("finding_not_object")
            continue

        if item.get("severity") not in SEVERITIES:
            errors.append("bad_severity")

        for field in ("file", "issue", "evidence"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                errors.append(f"missing_{field}")

    return errors


def discover_results(root: Path = DEFAULT_ROOT) -> list[Path]:
    outbox = Path(root) / "kimi" / "outbox"
    results = []

    if not outbox.is_dir():
        return results

    for path in sorted(outbox.iterdir()):
        if not path.is_file() or _reject_symlink(path):
            continue

        if FILENAME.match(path.name):
            results.append(path)

    return results


def archive(root: Path, round_number: int, source: Path) -> Path:
    target_dir = Path(root) / "archive" / f"round-{round_number:03d}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / Path(source).name

    if not target.exists():
        shutil.copyfile(source, target)

    return target


def _archive_request(root: Path, round_number: int) -> str | None:
    """轮次关闭时快照 request（已发送优先，其次未发送草稿）。"""

    for candidate in (
        inbox_request_path(root, round_number),
        draft_path(root, round_number),
    ):
        if candidate.is_file() and not _reject_symlink(candidate):
            target = archive(root, round_number, candidate)

            return str(Path(target).relative_to(Path(root)))

    return None


def _already_processed(state: dict, round_number: int, digest: str) -> bool:
    for item in state.get("processed_results") or []:
        if item.get("round") == round_number and item.get("sha256") == digest:
            return True

    return False


# ---------------------------------------------------------------- 生成物

def _findings_summary(payload: dict) -> str:
    lines: list[str] = []

    for item in payload.get("findings") or []:
        if not isinstance(item, dict):
            continue

        location = str(item.get("file") or "?")

        if item.get("line") is not None:
            location = f"{location}:{item['line']}"

        lines.append(f"- [{item.get('severity')}] {location} — {item.get('issue') or ''}")

        if item.get("evidence"):
            lines.append(f"  证据：{item['evidence']}")

        if item.get("suggestion"):
            lines.append(f"  建议：{item['suggestion']}")

    return "\n".join(lines) if lines else "- （无）"


def _target_of(payload: dict) -> str:
    target = payload.get("target")

    return target.strip() if isinstance(target, str) and target.strip() else "（未注明）"


def write_fix_task(root: Path, round_number: int, payload: dict) -> Path:
    findings = payload.get("findings") or []
    text = (
        f"# round-{round_number:03d} 修复任务\n\n"
        f"> 目标：{_target_of(payload)}\n"
        f"> 生成：{_now()}（自动生成，供人工 / PA 主 Agent 执行）\n"
        f"> 结论：findings {len(findings)} 条\n\n"
        "## findings\n\n"
        f"{_findings_summary(payload)}\n\n"
        "## 验收\n\n"
        "- 关闭所有 blocker/high\n"
        "- 重新跑全量\n"
        "- 下一轮请求走「草稿 → 确认 → 发送」\n"
    )
    path = fix_task_path(root, round_number)
    _write_text_atomic(path, text, prefix=".fix-task-")

    return path


def write_next_draft(root: Path, round_number: int, payload: dict) -> Path:
    previous = payload.get("round")
    findings = payload.get("findings") or []
    text = (
        f"# 复审请求 round-{round_number:03d}（草稿）\n\n"
        f"> 状态：**草稿**——本文件位于 `pa/outbox/drafts/`，移到 `kimi/inbox/` 才算发送；"
        f"发送前必须人工确认。\n"
        f"> 生成：{_now()}\n"
        f"> 目标：{_target_of(payload)}\n"
        f"> 上一轮：round-{int(previous or 0):03d} 结果 findings {len(findings)} 条\n\n"
        "## 本轮修了什么\n\n"
        "- （待填：PA 的修复摘要，逐条对应上轮 findings）\n\n"
        "## 请复审\n\n"
        f"- 目标文件：{_target_of(payload)}\n"
        "- 范围：（待填：本次复审的具体范围）\n"
        f"- 要求：按结果合同输出 JSON 到 `kimi/outbox/round-{round_number:03d}-result.json`；"
        "每条 finding 必须带 evidence；无问题则 `verdict: \"pass\"`。\n\n"
        "## 上一轮 findings\n\n"
        f"{_findings_summary(payload)}\n"
    )
    path = draft_path(root, round_number)
    _write_text_atomic(path, text)

    return path


def validate_draft(root: Path, round_number: int) -> list[str]:
    path = draft_path(root, round_number)
    errors: list[str] = []

    if not path.is_file() or _reject_symlink(path):
        return ["draft_missing"]

    size = path.stat().st_size

    if size < MIN_DRAFT_BYTES:
        errors.append("draft_too_small")

    if size > MAX_DRAFT_BYTES:
        errors.append("draft_too_large")

    if errors:
        return errors

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ["draft_unreadable"]

    if DRAFT_SECTION not in text:
        errors.append("draft_no_review_section")

    return errors


# ---------------------------------------------------------------- 通知

def _queue_notification(
    state: dict,
    round_number: int,
    event: str,
    text: str,
    notifier: Notifier | None,
) -> list[tuple[str, str]]:
    """去重标记；真正发送由调用方在 state 落盘之后做。"""

    if notifier is None:
        return []

    key = f"{round_number}:{event}"
    notified = state.setdefault("notified", {})

    if key in notified:
        return []

    notified[key] = _now()

    return [(event, text)]


def _deliver(
    notifier: Notifier | None, messages: list[tuple[str, str]]
) -> tuple[list[str], list[str]]:
    sent: list[str] = []
    failed: list[str] = []

    if notifier is None:
        return sent, failed

    for event, text in messages:
        try:
            notifier(text)
            sent.append(event)
        except Exception as error:  # noqa: BLE001 - 通知失败不能拖垮状态机
            failed.append(f"{event}:{error}")
            print(f"review_bus: 通知失败 {event}: {error}", file=sys.stderr)

    return sent, failed


# ---------------------------------------------------------------- 主流程

def process_once(
    root: Path = DEFAULT_ROOT,
    *,
    notifier: Notifier | None = None,
    max_rounds: int = MAX_ROUNDS,
) -> dict:
    root = Path(root)
    state = load_state(root)
    _ensure_state_defaults(state)

    if state.get("status") != "waiting_kimi":
        return {"ok": True, "action": "not_waiting", "status": state.get("status")}

    round_number = int(state.get("round", 0))
    candidates = [
        path for path in discover_results(root)
        if int(FILENAME.match(path.name).group(1)) == round_number
    ]

    if not candidates:
        return {"ok": True, "action": "no_result", "round": round_number}

    if len(candidates) > 1:
        return _fail(root, state, round_number, "multiple_results", notifier=notifier)

    path = candidates[0]

    if path.stat().st_size > MAX_RESULT_BYTES:
        return _fail(root, state, round_number, "result_too_large", notifier=notifier)

    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _fail(root, state, round_number, "invalid_json", notifier=notifier)

    digest = hashlib.sha256(raw).hexdigest()

    if _already_processed(state, round_number, digest):
        return {"ok": True, "action": "duplicate", "round": round_number}

    errors = validate_result(payload, state)

    if errors:
        return _fail(root, state, round_number, ",".join(errors), notifier=notifier)

    archived = archive(root, round_number, path)
    verdict = payload["verdict"]
    state["result_file"] = path.name
    state["target"] = _target_of(payload) if payload.get("target") else state.get("target")
    state["loop_round"] = int(state.get("loop_round") or 0) + 1
    loop_round = state["loop_round"]
    messages: list[tuple[str, str]] = []
    extra: dict = {}

    if verdict == "pass":
        state["status"] = "pass_waiting_human_signoff"
        _archive_request(root, round_number)
        messages += _queue_notification(
            state, round_number, "pass",
            f"[PA] round-{round_number:03d} 复审通过（pass），待你签字",
            notifier,
        )
    elif verdict == "findings":
        fix_task = write_fix_task(root, round_number, payload)
        state["fix_task_file"] = str(fix_task.relative_to(root))
        extra["fix_task"] = state["fix_task_file"]

        if loop_round < max_rounds:
            draft = write_next_draft(root, round_number + 1, payload)
            state["draft_file"] = str(draft.relative_to(root))
            state["status"] = "fixing"
            extra["draft"] = state["draft_file"]
            messages += _queue_notification(
                state, round_number, "findings",
                f"[PA] round-{round_number:03d} 有 {len(payload.get('findings') or [])} 条 findings；"
                f"修复任务与下一轮草稿已生成，待你确认发送",
                notifier,
            )
        else:
            state["draft_file"] = None
            state["status"] = "stopped"
            _archive_request(root, round_number)
            extra["stopped_reason"] = "max_rounds"
            messages += _queue_notification(
                state, round_number, "stopped",
                f"[PA] round-{round_number:03d} 第 {loop_round} 轮仍有 findings，"
                f"已达上限 {max_rounds} 轮，停止",
                notifier,
            )
    else:
        state["status"] = "stopped"
        _archive_request(root, round_number)
        extra["stopped_reason"] = "blocked"
        messages += _queue_notification(
            state, round_number, "stopped",
            f"[PA] round-{round_number:03d} 被判定 blocked，停止",
            notifier,
        )

    state["updated_at"] = _now()
    state.setdefault("processed_results", []).append(
        {"round": round_number, "sha256": digest, "verdict": verdict}
    )
    _history(
        state, round_number, "result_processed",
        verdict=verdict, file=path.name,
        archived=str(archived.relative_to(root)), loop_round=loop_round,
    )

    if extra.get("stopped_reason"):
        _history(state, round_number, "stopped", reason=extra["stopped_reason"])

    save_state_atomic(root, state)
    sent, failed = _deliver(notifier, messages)

    return {
        "ok": True,
        "action": "processed",
        "round": round_number,
        "verdict": verdict,
        "status": state["status"],
        "loop_round": loop_round,
        "notified": sent,
        "notify_failed": failed,
        **extra,
    }


def _fail(
    root: Path,
    state: dict,
    round_number: int,
    error: str,
    *,
    notifier: Notifier | None = None,
) -> dict:
    _history(state, round_number, "review_failed", error=error)
    messages = _queue_notification(
        state, round_number, "failed",
        f"[PA] round-{round_number:03d} 结果校验失败：{error}",
        notifier,
    )
    state["updated_at"] = _now()
    save_state_atomic(root, state)
    sent, failed = _deliver(notifier, messages)

    return {
        "ok": False,
        "action": "review_failed",
        "errors": [error],
        "notified": sent,
        "notify_failed": failed,
    }


def confirm_send(
    root: Path = DEFAULT_ROOT,
    round_number: int | None = None,
    *,
    notifier: Notifier | None = None,
) -> dict:
    """人工确认后把草稿移入 kimi/inbox —— 这是唯一的「发送」动作。"""

    root = Path(root)
    state = load_state(root)
    _ensure_state_defaults(state)

    if state.get("status") != "fixing":
        return {"ok": False, "action": "not_fixing", "status": state.get("status")}

    expected = int(state.get("round", 0)) + 1

    if round_number is not None and int(round_number) != expected:
        return {
            "ok": False, "action": "round_mismatch",
            "expected": expected, "received": int(round_number),
        }

    errors = validate_draft(root, expected)

    if errors:
        return {"ok": False, "action": "draft_invalid", "errors": errors}

    source = draft_path(root, expected)
    target = inbox_request_path(root, expected)

    if target.exists():
        return {"ok": False, "action": "target_exists", "path": str(target)}

    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)

    try:
        # 让文件 mtime 也等于发送时刻，避免「文件时间」与「state 时间」两个口径打架
        os.utime(target, None)
    except OSError:
        pass

    try:
        state["round"] = expected
        state["request_file"] = target.name
        # 计时起点用「发送动作时刻」：os.replace 会保留草稿的 mtime，
        # 拿它当发送时间会把等人工确认的时间也算进超时。
        state["request_sent_at"] = _now()
        state["draft_file"] = None
        state["status"] = "waiting_kimi"
        state["updated_at"] = _now()
        _history(state, expected, "request_sent", file=target.name)
        messages = _queue_notification(
            state, expected, "start",
            f"[PA] 复审批次 round-{expected:03d} 已发送：{state.get('target') or '（未注明）'}",
            notifier,
        )
        save_state_atomic(root, state)
    except Exception:
        # 状态没写成功就把文件退回去，避免「已发送但状态没推进」的半截状态
        try:
            os.replace(target, source)
        except OSError:
            pass
        raise

    sent, failed = _deliver(notifier, messages)

    return {
        "ok": True,
        "action": "sent",
        "round": expected,
        "request": str(target.relative_to(root)),
        "notified": sent,
        "notify_failed": failed,
    }


def reconcile(
    root: Path = DEFAULT_ROOT, *, notifier: Notifier | None = None
) -> dict:
    """启动对账：kimi/inbox 已有下一轮 request 但状态没推进时补推进。"""

    root = Path(root)
    state = load_state(root)
    _ensure_state_defaults(state)

    if state.get("status") != "fixing":
        return {"ok": True, "action": "not_applicable", "status": state.get("status")}

    current = int(state.get("round", 0))
    inbox = root / "kimi" / "inbox"
    pending: list[tuple[int, Path]] = []

    if inbox.is_dir():
        for path in inbox.iterdir():
            if not path.is_file() or _reject_symlink(path):
                continue

            match = REQUEST_FILENAME.match(path.name)

            if match and int(match.group(1)) > current:
                pending.append((int(match.group(1)), path))

    if not pending:
        return {"ok": True, "action": "nothing_to_reconcile"}

    pending.sort()

    if len({number for number, _ in pending}) > 1:
        return {"ok": False, "action": "ambiguous_requests"}

    number, path = pending[0]
    state["round"] = number
    state["request_file"] = path.name
    state["request_sent_at"] = _iso(path.stat().st_mtime)
    state["draft_file"] = None
    state["status"] = "waiting_kimi"
    state["updated_at"] = _now()
    _history(
        state, number, "request_sent_recovered",
        file=path.name, note="rename 成功但 state 未推进的补账",
    )
    messages = _queue_notification(
        state, number, "start",
        f"[PA] 复审批次 round-{number:03d} 已发送（补账）",
        notifier,
    )
    save_state_atomic(root, state)
    sent, failed = _deliver(notifier, messages)

    return {
        "ok": True, "action": "recovered", "round": number,
        "notified": sent, "notify_failed": failed,
    }


def stop(
    root: Path = DEFAULT_ROOT, reason: str = "manual", *, notifier: Notifier | None = None
) -> dict:
    root = Path(root)
    state = load_state(root)
    _ensure_state_defaults(state)

    if state.get("status") in ("stopped", "signed_off"):
        return {"ok": True, "action": "already_stopped", "status": state.get("status")}

    round_number = int(state.get("round", 0))
    _archive_request(root, round_number)
    state["status"] = "stopped"
    state["updated_at"] = _now()
    _history(state, round_number, "stopped", reason=reason)
    messages = _queue_notification(
        state, round_number, "stopped",
        f"[PA] 复审已停止：{reason}（round-{round_number:03d}）",
        notifier,
    )
    save_state_atomic(root, state)
    sent, failed = _deliver(notifier, messages)

    return {
        "ok": True, "action": "stopped", "reason": reason, "round": round_number,
        "notified": sent, "notify_failed": failed,
    }


def check_timeout(
    root: Path = DEFAULT_ROOT,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    now: float | None = None,
    notifier: Notifier | None = None,
) -> dict:
    """等待结果超时 → stopped。计时起点是 request 落入 kimi/inbox 的 mtime。"""

    root = Path(root)
    state = load_state(root)
    _ensure_state_defaults(state)

    if state.get("status") != "waiting_kimi":
        return {"ok": True, "action": "not_waiting", "status": state.get("status")}

    round_number = int(state.get("round", 0))
    baseline = _to_timestamp(state.get("request_sent_at"))

    if baseline is None:
        path = inbox_request_path(root, round_number)

        if path.is_file():
            baseline = path.stat().st_mtime

    if baseline is None:
        baseline = _to_timestamp(state.get("started_at"))

    if baseline is None:
        return {"ok": True, "action": "no_baseline"}

    current = time.time() if now is None else float(now)
    remaining = timeout - (current - baseline)

    if remaining > 0:
        return {"ok": True, "action": "waiting", "remaining": round(remaining, 1)}

    return stop(root, f"超过 {int(timeout)} 秒没有结果", notifier=notifier)
