"""M1 文件总线：发现 Kimi 结果、校验、归档、推进 review 状态。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile

from datetime import datetime, timezone
from pathlib import Path


DEFAULT_ROOT = Path(r"C:\Users\123\PA-Shared")
MAX_RESULT_BYTES = 2 * 1024 * 1024
FILENAME = re.compile(r"^round-(\d{3})-result\.json$")
VERDICTS = ("pass", "findings", "blocked")
SEVERITIES = ("blocker", "high", "medium", "low")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state_path(root: Path) -> Path:
    return root / "state" / "review.json"


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


def _reject_symlink(path: Path) -> bool:
    return path.is_symlink() or bool(
        getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400
    )


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
    outbox = root / "kimi" / "outbox"
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
    target_dir = root / "archive" / f"round-{round_number:03d}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    shutil.copyfile(source, target)

    return target


def _already_processed(state: dict, round_number: int, digest: str) -> bool:
    for item in state.get("processed_results") or []:
        if item.get("round") == round_number and item.get("sha256") == digest:
            return True

    return False


def process_once(root: Path = DEFAULT_ROOT) -> dict:
    root = Path(root)
    state = load_state(root)

    if state.get("status") != "waiting_kimi":
        return {"ok": True, "action": "not_waiting", "status": state.get("status")}

    round_number = int(state.get("round", 0))
    candidates = [
        path for path in discover_results(root)
        if FILENAME.match(path.name)
        and int(FILENAME.match(path.name).group(1)) == round_number
    ]

    if not candidates:
        return {"ok": True, "action": "no_result", "round": round_number}

    if len(candidates) > 1:
        state.setdefault("history", []).append({
            "round": round_number, "event": "review_failed",
            "by": "pa", "at": _now(), "error": "multiple_results",
        })
        save_state_atomic(root, state)
        return {"ok": False, "action": "review_failed", "errors": ["multiple_results"]}

    path = candidates[0]

    if path.stat().st_size > MAX_RESULT_BYTES:
        return _fail(root, state, round_number, "result_too_large")

    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _fail(root, state, round_number, "invalid_json")

    digest = hashlib.sha256(raw).hexdigest()

    if _already_processed(state, round_number, digest):
        return {"ok": True, "action": "duplicate", "round": round_number}

    errors = validate_result(payload, state)

    if errors:
        return _fail(root, state, round_number, ",".join(errors))

    archived = archive(root, round_number, path)
    verdict = payload["verdict"]
    state["result_file"] = path.name
    state["status"] = (
        "pass_waiting_human_signoff" if verdict == "pass" else "fixing"
    )
    state["updated_at"] = _now()
    state.setdefault("processed_results", []).append({
        "round": round_number, "sha256": digest, "verdict": verdict,
    })
    state.setdefault("history", []).append({
        "round": round_number, "event": "result_processed",
        "by": "pa", "at": _now(), "verdict": verdict,
        "file": path.name, "archived": str(archived.relative_to(root)),
    })
    save_state_atomic(root, state)

    return {
        "ok": True, "action": "processed", "round": round_number,
        "verdict": verdict, "status": state["status"],
    }


def _fail(root: Path, state: dict, round_number: int, error: str) -> dict:
    state.setdefault("history", []).append({
        "round": round_number, "event": "review_failed",
        "by": "pa", "at": _now(), "error": error,
    })
    save_state_atomic(root, state)

    return {"ok": False, "action": "review_failed", "errors": [error]}
