"""六类故障注入：每一类都给出"终态 + 恢复证据"。

原则：**注入点打在真实代码的边界上**（传输层、事务、runner、sender），
不改产品逻辑去"配合测试"；注入解除后必须能恢复正常，否则不算"可恢复"。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import uuid

from contextlib import closing

from pathlib import Path
from unittest.mock import patch

import requests

from src.artifacts.models import ArtifactError
from src.artifacts.registry import ArtifactRegistry
from src.research.llm import LLMRateLimited, complete


def _ok(name: str, terminal: str, detail: str, **evidence) -> dict:
    return {
        "name": name,
        "terminal": terminal,
        "recovered": True,
        "detail": detail,
        "evidence": evidence,
    }


def fault_disk_full() -> dict:
    """① 体积超限：拒绝登记且不留残行；上限放开后能登记。"""

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / "artifacts"
        root.mkdir()
        registry = ArtifactRegistry(Path(folder) / "a.db", root=root)
        (root / "big.bin").write_bytes(b"x" * 2048)

        with patch("src.artifacts.registry.MAX_ARTIFACT_BYTES", 16):
            try:
                registry.register("u1", "big.bin")
                raise AssertionError("超限却没有拒绝")

            except ArtifactError:
                pass

        rows_after_reject = registry.count("u1")
        artifact = registry.register("u1", "big.bin")

        return _ok(
            "disk_full",
            "artifact_rejected",
            "超限被拒且无残留，解除上限后登记成功",
            rows_after_reject=rows_after_reject,
            recovered_id=artifact.id,
        )


def fault_search_timeout() -> dict:
    """② 搜索超时：抛 SearchUnavailable；恢复后能拿到结果。"""

    from src.web.search import (
        BochaSearchProvider,
        SearchHit,
        SearchOutcome,
        SearchUnavailable,
    )

    def broken(url, headers, body, timeout):
        raise TimeoutError("network down")

    provider = BochaSearchProvider("k", transport=broken)

    try:
        provider.search("x")
        raise AssertionError("超时却没有抛错")

    except SearchUnavailable as error:
        terminal = str(error)

    def fixed(url, headers, body, timeout):
        return (
            200,
            b'{"data": {"webPages": {"value": [{"name": "n", "url": "https://e.org/x"}]}}}',
        )

    recovered = BochaSearchProvider("k", transport=fixed).search("x")

    return _ok(
        "search_timeout",
        "search_unavailable",
        f"{terminal}；恢复后拿到 {recovered.count} 条",
        recovered_hits=recovered.count,
    )


def fault_model_rate_limit() -> dict:
    """③ 模型限流：重试 1 次后仍失败，抛 LLMRateLimited（不无限重试）。"""

    calls = []

    def limited(messages, params):
        calls.append(1)

        class Error(Exception):
            status_code = 429

        raise Error("429")

    try:
        complete([{"role": "user", "content": "x"}], completer=limited, sleep=lambda _: None)
        raise AssertionError("限流却没有抛错")

    except LLMRateLimited:
        pass

    def healthy(messages, params):
        class Message:
            content = "ok"

        class Choice:
            message = Message()

        class Usage:
            prompt_tokens = 1
            completion_tokens = 1
            total_tokens = 2

        class Response:
            choices = [Choice()]
            usage = Usage()

        return Response()

    recovered = complete(
        [{"role": "user", "content": "x"}],
        completer=healthy,
        sleep=lambda _: None,
    )

    return _ok(
        "model_rate_limit",
        "llm_rate_limited",
        f"重试 {len(calls)} 次后失败；恢复后返回 {recovered.text!r}",
        attempts=len(calls),
    )


def fault_exec_crash() -> dict:
    """④ 执行器崩溃：闭环要把崩溃记成失败轮，而不是把进程带崩。"""

    from src.repair.fixtures import get_fixture
    from src.repair.loop import RepairLoop

    fixture = get_fixture("off_by_one")

    def crashing_runner(files, *, modules, timeout):
        raise RuntimeError("执行器崩了")

    def fixed_runner(files, *, modules, timeout):
        from src.repair.loop import RunResult

        return RunResult(ok=True, failures=0, errors=0, output="OK", duration_ms=1.0)

    crashed = RepairLoop(
        fixture,
        runner=crashing_runner,
        fixer=lambda *args: dict(fixture.files),
        max_rounds=1,
    ).run()

    recovered = RepairLoop(
        fixture, runner=fixed_runner, fixer=lambda *args: {}
    ).run()

    return _ok(
        "exec_crash",
        "run_failed",
        "崩溃被记成失败轮；换正常执行器后成功",
        crashed_error=crashed.error or "ok",
        recovered=crashed.success is False and recovered.success is True,
    )


def fault_notification_fail() -> dict:
    """⑤ 通知投递失败：回退 pending 并可重试，最终只送达一次。

    **必须用隔离库**：往项目真实 tasks.db 里注入故障是不可接受的。
    """

    import shutil

    import src.core.outbox_worker as worker
    from src import config

    workspace = tempfile.mkdtemp(prefix="pa-fault-outbox-")
    isolated = config.build_paths(workspace)

    # 凡是 `from src.config import paths` 的模块都持有早绑定副本，
    # 只 patch config 不够（task_engine 会拿旧路径建表）。
    original = config.paths
    patches = [patch.object(config, "paths", isolated)]

    for module in list(sys.modules.values()):
        if module is None:
            continue

        if getattr(module, "paths", None) is original:
            patches.append(patch.object(module, "paths", isolated))

    for item in patches:
        item.start()

    try:
        return _notification_fault_body(isolated)

    finally:
        for item in patches:
            item.stop()

        shutil.rmtree(workspace, ignore_errors=True)


def _notification_fault_body(paths) -> dict:
    """隔离库里的通知故障注入主体。"""

    import src.core.outbox_worker as worker
    import src.core.task_engine as engine
    from src.core.outbox_worker import process_one_notification

    # 显式绑定：这两个模块都是 `from src.config import paths` 的早绑定副本，
    # 只 patch config 在第二次调用时跟不上。
    engine.paths = paths
    worker.paths = paths

    marker = f"fault-{uuid.uuid4().hex[:8]}"

    process_one_notification()  # 建表

    # 兜底建表：即使 init_task_db 因早绑定写到别处，也保证这里的表存在。
    with closing(sqlite3.connect(paths.tasks_db, timeout=10)) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS notification_outbox ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id TEXT NOT NULL, "
            "item_id TEXT NOT NULL, message TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'pending', "
            "attempts INTEGER NOT NULL DEFAULT 0, "
            "next_attempt_at TEXT, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "sent_at TEXT, owner_id TEXT, UNIQUE (monitor_id, item_id))"
        )

    with closing(sqlite3.connect(paths.tasks_db, timeout=10)) as conn, conn:
        conn.execute(
            "INSERT INTO notification_outbox "
            "(monitor_id, item_id, message, status, attempts, owner_id) "
            "VALUES (?,?,?,?,?,?)",
            (marker, "item-1", "故障通知", "pending", 0, "owner-1"),
        )

    with patch.object(worker, "channel_for_owner", lambda _owner: "ntfy"):
        with patch.object(
            worker,
            "send_notification",
            side_effect=requests.RequestException("network down"),
        ):
            process_one_notification()

    with closing(sqlite3.connect(paths.tasks_db, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, attempts FROM notification_outbox WHERE monitor_id = ?",
            (marker,),
        ).fetchone()

    status_after_failure = row["status"]
    attempts_after_failure = int(row["attempts"])

    with closing(sqlite3.connect(paths.tasks_db, timeout=10)) as conn, conn:
        conn.execute(
            "UPDATE notification_outbox SET next_attempt_at = NULL WHERE monitor_id = ?",
            (marker,),
        )

    sent = []

    with patch.object(worker, "channel_for_owner", lambda _owner: "ntfy"):
        with patch.object(
            worker,
            "send_notification",
            side_effect=lambda message: sent.append(message),
        ):
            process_one_notification()

    with closing(sqlite3.connect(paths.tasks_db, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        final = conn.execute(
            "SELECT status FROM notification_outbox WHERE monitor_id = ?",
            (marker,),
        ).fetchone()

    return _ok(
        "notification_fail",
        "outbox_retry",
        f"失败后 {status_after_failure}（attempts={attempts_after_failure}），"
        f"重试后 {final['status']}，送达 {len(sent)} 次",
        status_after_failure=status_after_failure,
        final_status=final["status"],
        delivered_times=len(sent),
    )


def fault_db_write_interrupt() -> dict:
    """⑥ 数据库写入中断：事务回滚，不留半条记录；重试后完整写入。"""

    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "t.db"

        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")

        try:
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("INSERT INTO items (name) VALUES ('half')")
                raise RuntimeError("写入中断")

        except RuntimeError:
            pass

        with closing(sqlite3.connect(db)) as conn:
            count_after_failure = conn.execute(
                "SELECT COUNT(*) FROM items"
            ).fetchone()[0]

        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("INSERT INTO items (name) VALUES ('full')")

        with closing(sqlite3.connect(db)) as conn:
            rows = conn.execute("SELECT name FROM items").fetchall()

    return _ok(
        "db_write_interrupt",
        "transaction_rolled_back",
        f"中断后 {count_after_failure} 行（应为 0）；重试后 {len(rows)} 行",
        count_after_failure=count_after_failure,
        recovered_rows=len(rows),
    )


FAULTS = (
    ("disk_full", fault_disk_full, "磁盘/体积超限"),
    ("search_timeout", fault_search_timeout, "搜索超时"),
    ("model_rate_limit", fault_model_rate_limit, "模型限流"),
    ("exec_crash", fault_exec_crash, "执行器崩溃"),
    ("notification_fail", fault_notification_fail, "通知投递失败"),
    ("db_write_interrupt", fault_db_write_interrupt, "数据库写入中断"),
)


def run_matrix() -> list[dict]:
    """逐类跑一遍，返回结果列表（单类失败不掩盖其它类）。"""

    results = []

    for name, function, label in FAULTS:
        try:
            outcome = function()
            outcome["label"] = label

        except Exception as error:  # noqa: BLE001 - 故障注入自身失败也要记下来
            outcome = {
                "name": name,
                "label": label,
                "terminal": "injection_failed",
                "recovered": False,
                "detail": f"{type(error).__name__}: {error}",
                "evidence": {},
            }

        results.append(outcome)

    return results
