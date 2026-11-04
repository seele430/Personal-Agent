"""D11 tier1 runner：工作区隔离、环境清洗、超时、输出上限与进程树清理。

这些用例都会创建**真实 OS 进程**，因此：

- 每个用例都设短超时，失败也不会挂住；
- ``tearDown`` 逐个确认探针写出的 PID 已经消失，不允许把子进程留在机器上；
- 只使用临时目录里的数据库，不碰项目根目录的 ``*.db``。
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import unittest

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src import agent, config
from src.core.run_control import CancellationToken
from src.platform.exec_service import (
    ERROR_CANCELLED,
    ERROR_OUTPUT_LIMIT,
    ERROR_TIMED_OUT,
    ERROR_TOOL,
    EXEC_CANCELLED,
    EXEC_FAILED,
    EXEC_SUCCEEDED,
    EXEC_TIMED_OUT,
    Tier1Executor,
)
from src.platform.process_tree import (
    CLEANUP_JOB_OBJECT,
    CLEANUP_PROCESS_ONLY,
    process_is_alive,
)
from tests.helper import WINDOWS, isolate_paths, windows_only
from tests.integration.tier1_probe import (
    probe_argv,
    read_pids,
    wait_for_pids,
)


class Tier1RunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        self.root = Path(self.temp.name) / "sandboxes"
        self.python = sys.executable
        self.executor = Tier1Executor(root=self.root)
        self.pid_files: list[Path] = []

    def pid_file(self, name: str = "pids.json") -> Path:
        path = Path(self.temp.name) / name
        self.pid_files.append(path)
        return path

    def tearDown(self):
        # 用例失败也不能留下进程；这是 D11 的硬约束。
        leftovers = []

        for path in self.pid_files:
            if not path.is_file():
                continue
            try:
                pids = read_pids(path)
            except (OSError, json.JSONDecodeError):
                continue
            for pid in pids.values():
                if process_is_alive(pid):
                    leftovers.append(pid)

        if not WINDOWS:
            # 非 Windows 只有 process_only 降级：平台保证不了「整棵树」，
            # 孙进程由测试自己收掉，否则会污染后续用例。
            for pid in leftovers:
                force_kill(pid)
            return

        self.assertEqual(leftovers, [], f"遗留进程：{leftovers}")

    # ------------------------------------------------------------------
    # 工作区与环境
    # ------------------------------------------------------------------

    def test_child_runs_in_platform_workspace(self):
        outcome = self.executor.execute_command(
            [self.python, "-c", "import os;print(os.getcwd())"]
        )

        self.assertEqual(outcome.status, EXEC_SUCCEEDED, outcome.stderr)

        # 清理模式按平台降级，但必须如实写进 outcome，不能冒充 job 级清理。
        expected_cleanup = CLEANUP_JOB_OBJECT if WINDOWS else CLEANUP_PROCESS_ONLY
        self.assertEqual(outcome.cleanup, expected_cleanup)

        workspace = Path(outcome.stdout.strip())
        self.assertTrue(
            str(workspace).startswith(str(self.root)),
            f"工作区不在平台根目录下：{workspace}",
        )
        self.assertNotEqual(workspace, Path.cwd())

    def test_child_environment_is_cleaned(self):
        poison = {
            "D11_POISON": "should-not-leak",
            "OPENAI_API_KEY": "sk-should-not-leak",
            "PERSONAL_AGENT_DATA_DIR": r"C:\should-not-leak",
            "PYTHONPATH": r"C:\should-not-leak",
        }

        with patch.dict("os.environ", poison, clear=False):
            outcome = self.executor.execute_command(
                [self.python, "-c", "import json,os;print(json.dumps(dict(os.environ)))"]
            )

        self.assertEqual(outcome.status, EXEC_SUCCEEDED, outcome.stderr)

        child_env = json.loads(outcome.stdout.strip())

        for name in poison:
            self.assertNotIn(name, child_env, f"{name} 泄漏进 tier1")

        for name, value in child_env.items():
            self.assertNotIn("should-not-leak", value, f"{name} 带有宿主值")

        self.assertEqual(child_env.get("PERSONAL_AGENT_TIER1"), "1")

    # ------------------------------------------------------------------
    # 工具在子进程里执行
    # ------------------------------------------------------------------

    def test_tool_runs_in_child_not_in_host(self):
        identity = {
            "user_id": "alice",
            "request_id": "d11_runner_tool",
            "role": "member",
        }

        with patch.object(
            agent,
            "_execute_reminder_tool",
            side_effect=AssertionError("宿主进程不得直接执行工具"),
        ) as host_handler:
            outcome = self.executor.execute_tool(
                "create_reminder",
                {"message": "喝水", "minutes": 5},
                identity=identity,
            )

        self.assertEqual(outcome.status, EXEC_SUCCEEDED, outcome.stderr)
        self.assertTrue(outcome.result["success"])
        self.assertGreater(outcome.result["reminder_id"], 0)
        host_handler.assert_not_called()

        with closing(sqlite3.connect(config.paths.reminders_db)) as conn:
            rows = conn.execute(
                """
                SELECT user_id, status, delivery
                FROM reminder_requests WHERE user_id = ?
                """,
                ("alice",),
            ).fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "alice")
        self.assertEqual(rows[0][1], "pending")
        self.assertEqual(rows[0][2], "in_app")

    def test_tool_error_is_reported_not_masked_as_crash(self):
        identity = {
            "user_id": "alice",
            "request_id": "d11_runner_error",
            "role": "member",
        }

        outcome = self.executor.execute_tool(
            "create_reminder",
            {"message": "x" * 2000, "minutes": 5},
            identity=identity,
        )

        self.assertEqual(outcome.status, EXEC_FAILED)
        self.assertEqual(outcome.error_code, ERROR_TOOL)
        self.assertEqual(outcome.tool_error["error_type"], "ValueError")

    # ------------------------------------------------------------------
    # 超时 / 输出上限 / 取消：都要清整棵树
    # ------------------------------------------------------------------

    @windows_only("整棵进程树清理依赖 Windows Job Object")
    def test_timeout_terminates_whole_tree(self):
        pid_file = self.pid_file()

        outcome = self.executor.execute_command(
            probe_argv(self.python, pid_file), timeout_ms=1500
        )

        self.assertEqual(outcome.status, EXEC_TIMED_OUT)
        self.assertEqual(outcome.error_code, ERROR_TIMED_OUT)

        pids = wait_for_pids(pid_file)
        self.assertFalse(process_is_alive(pids["parent"]))
        self.assertFalse(process_is_alive(pids["child"]))

    @windows_only("整棵进程树清理依赖 Windows Job Object")
    def test_output_limit_terminates_whole_tree(self):
        pid_file = self.pid_file()

        outcome = self.executor.execute_command(
            probe_argv(self.python, pid_file, mode="flood"),
            max_output_bytes=2000,
            timeout_ms=20000,
        )

        self.assertEqual(outcome.status, EXEC_FAILED)
        self.assertEqual(outcome.error_code, ERROR_OUTPUT_LIMIT)
        self.assertTrue(outcome.truncated)
        self.assertLessEqual(len(outcome.stdout), 2000)

        pids = wait_for_pids(pid_file)
        self.assertFalse(process_is_alive(pids["parent"]))
        self.assertFalse(process_is_alive(pids["child"]))

    @windows_only("整棵进程树清理依赖 Windows Job Object")
    def test_cancel_terminates_whole_tree(self):
        pid_file = self.pid_file()
        token = CancellationToken()

        timer = threading.Timer(1.0, token.cancel)
        timer.start()
        self.addCleanup(timer.cancel)

        outcome = self.executor.execute_command(
            probe_argv(self.python, pid_file),
            cancel_token=token,
            timeout_ms=30000,
        )

        self.assertEqual(outcome.status, EXEC_CANCELLED)
        self.assertEqual(outcome.error_code, ERROR_CANCELLED)
        self.assertIsNotNone(outcome.cancel_requested_at)
        self.assertEqual(outcome.cleanup, CLEANUP_JOB_OBJECT)

        pids = wait_for_pids(pid_file)
        self.assertFalse(process_is_alive(pids["parent"]))
        self.assertFalse(process_is_alive(pids["child"]))

    def test_already_cancelled_token_starts_no_process(self):
        token = CancellationToken()
        token.cancel()

        outcome = self.executor.execute_command(
            [self.python, "-c", "print('never')"], cancel_token=token
        )

        self.assertEqual(outcome.status, EXEC_CANCELLED)
        self.assertEqual(outcome.stdout, "")


def force_kill(pid: int) -> None:
    """测试兜底：按 PID 结束残留进程，不参与平台能力断言。"""

    if WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


if __name__ == "__main__":
    unittest.main()
