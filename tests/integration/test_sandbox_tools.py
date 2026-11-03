"""D12 沙箱工具：read / write / ls / shell / python / http 与路径负向测试。

全部用例都走真实 tier1 子进程：工具 handler 在 runner 里执行，宿主只分派。
路径负向用例覆盖 ``..``、绝对路径、盘符路径和符号链接逃逸 —— 判断依据是
解析后的 real path，不是字符串前缀。
"""

from __future__ import annotations

import os
import subprocess
import unittest

from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src import agent
from src.agent_stream import run_chat_stream
from src.core.reminder_identity import bind_authenticated_reminder
from src.core.run_control import CancellationToken
from src.core.run_store import RunStore
from src.platform.exec_service import Tier1Executor
from src.tools.sandbox_tools import MAX_READ_BYTES
from tests.helper import isolate_paths


def chunk(*, text=None, finish=None, tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=tool_calls or []),
                finish_reason=finish,
            )
        ]
    )


def tool_delta(name=None, arguments=None, call_id=None):
    return SimpleNamespace(
        index=0,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class SandboxToolsTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        self.root = Path(self.temp.name) / "sandboxes"
        self.executor = Tier1Executor(root=self.root)

        # 助手链路用 default_executor()，把平台根目录也指到临时目录。
        patcher = patch.dict(
            "os.environ",
            {"PERSONAL_AGENT_SANDBOX_ROOT": str(self.root)},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.identity = {
            "user_id": "alice",
            "request_id": "d12_tools",
            "role": "member",
        }
        self.sandbox = self.executor.open_sandbox()
        self.addCleanup(self.executor.destroy, self.sandbox)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def run_tool(self, name, arguments, sandbox_id=None):
        return self.executor.execute_tool(
            name,
            arguments,
            identity=self.identity,
            sandbox_id=sandbox_id or self.sandbox,
        )

    def ok(self, outcome) -> dict:
        self.assertEqual(
            outcome.status, "succeeded", outcome.stderr or outcome.tool_error
        )
        return outcome.result

    def tool_error(self, outcome, contains: str = "") -> dict:
        self.assertEqual(outcome.status, "failed")
        self.assertIsNotNone(outcome.tool_error, outcome.stderr)
        self.assertEqual(outcome.tool_error["error_type"], "ValueError")
        if contains:
            self.assertIn(contains, outcome.tool_error["message"])
        return outcome.tool_error

    @property
    def workspace(self) -> Path:
        return self.root / self.sandbox / "workspace"

    # ------------------------------------------------------------------
    # 文件工具
    # ------------------------------------------------------------------

    def test_write_and_read_share_one_sandbox(self):
        written = self.ok(
            self.run_tool(
                "write_file", {"path": "notes/hello.txt", "content": "你好 tier1"}
            )
        )
        self.assertEqual(written["path"], "notes/hello.txt")

        read = self.ok(self.run_tool("read_file", {"path": "notes/hello.txt"}))

        self.assertEqual(read["content"], "你好 tier1")
        self.assertEqual(read["size_bytes"], len("你好 tier1".encode("utf-8")))
        self.assertFalse(read["truncated"])

        # 文件真的落在本沙箱工作区里，而不是宿主仓库
        self.assertTrue((self.workspace / "notes" / "hello.txt").is_file())

    def test_list_dir_reports_entries(self):
        self.ok(self.run_tool("write_file", {"path": "a.txt", "content": "x"}))
        self.ok(self.run_tool("write_file", {"path": "sub/b.txt", "content": "y"}))

        listing = self.ok(self.run_tool("list_dir", {"path": "."}))

        names = {entry["name"]: entry["type"] for entry in listing["entries"]}
        self.assertEqual(names, {"a.txt": "file", "sub": "dir"})

    def test_read_truncates_large_file(self):
        big = self.workspace / "big.txt"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_bytes(b"x" * (MAX_READ_BYTES + 100))

        read = self.ok(self.run_tool("read_file", {"path": "big.txt"}))

        self.assertTrue(read["truncated"])
        self.assertEqual(len(read["content"]), MAX_READ_BYTES)

    def test_write_rejects_oversized_content(self):
        outcome = self.run_tool(
            "write_file", {"path": "too-big.txt", "content": "x" * 40000}
        )

        self.tool_error(outcome, "超过上限")

    # ------------------------------------------------------------------
    # 路径负向
    # ------------------------------------------------------------------

    def test_traversal_and_absolute_paths_are_rejected(self):
        cases = (
            "../outside.txt",
            "notes/../../outside.txt",
            "C:\\Windows\\win.ini",
            "/etc/passwd",
            "\\\\server\\share\\x.txt",
        )

        for raw in cases:
            with self.subTest(path=raw):
                self.tool_error(
                    self.run_tool("read_file", {"path": raw}), "路径"
                )
                self.tool_error(
                    self.run_tool(
                        "write_file", {"path": raw, "content": "x"}
                    ),
                    "路径",
                )

    def test_symlink_escape_is_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("secret", encoding="utf-8")

        link = self.workspace / "escape"

        if not self._make_directory_link(outside, link):
            self.skipTest("当前环境既不能建符号链接，也不能建目录联接")

        self.tool_error(
            self.run_tool("read_file", {"path": "escape/secret.txt"}), "路径"
        )
        self.tool_error(self.run_tool("list_dir", {"path": "escape"}), "路径")
        self.tool_error(
            self.run_tool(
                "write_file", {"path": "escape/new.txt", "content": "x"}
            ),
            "路径",
        )

        # 拒绝之后，工作区外的东西一个字节都不能变。
        self.assertEqual(secret.read_text(encoding="utf-8"), "secret")
        self.assertFalse((outside / "new.txt").exists())

    @staticmethod
    def _make_directory_link(target: Path, link: Path) -> bool:
        """优先符号链接；没有权限时退回目录联接（junction 不需要管理员）。"""

        try:
            os.symlink(target, link, target_is_directory=True)
            return True
        except (OSError, NotImplementedError):
            pass

        try:
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                check=False,
            )
        except OSError:
            return False

        return completed.returncode == 0

    # ------------------------------------------------------------------
    # 执行工具
    # ------------------------------------------------------------------

    def test_shell_reports_exit_code(self):
        ok = self.ok(self.run_tool("run_shell", {"command": "echo shell-ok"}))
        self.assertEqual(ok["exit_code"], 0)
        self.assertIn("shell-ok", ok["stdout"])

        failed = self.ok(self.run_tool("run_shell", {"command": "exit 3"}))
        self.assertEqual(failed["exit_code"], 3)

    def test_python_runs_in_sandbox_workspace(self):
        result = self.ok(
            self.run_tool(
                "run_python",
                {"code": "import os; print(os.path.basename(os.getcwd()))"},
            )
        )

        self.assertEqual(result["exit_code"], 0)
        self.assertIn("workspace", result["stdout"])

    def test_http_get_failure_is_a_tool_error(self):
        # 只连本机拒绝端口，不产生任何外部网络流量。
        outcome = self.run_tool("http_get", {"url": "http://127.0.0.1:1/nope"})

        self.tool_error(outcome, "HTTP 请求失败")

    # ------------------------------------------------------------------
    # 助手链路：每个工具 = 一次 exec + trajectory
    # ------------------------------------------------------------------

    def test_assistant_tool_call_produces_exactly_one_exec_and_trajectory(self):
        request_id = "d12_assistant_list_dir"
        events: list[dict] = []
        token = CancellationToken()

        identity = bind_authenticated_reminder(
            user_id="alice", request_id=request_id, role="member"
        )
        identity.__enter__()
        self.addCleanup(identity.__exit__, None, None, None)

        stream = iter(
            [
                chunk(tool_calls=[tool_delta(name="list_dir", call_id="call-d12")]),
                chunk(tool_calls=[tool_delta(arguments="{}")], finish="tool_calls"),
            ]
        )
        final = iter([chunk(text="工作区是空的", finish="stop")])

        with (
            patch.object(agent, "init_db"),
            patch.object(agent, "_knowledge_context", return_value=None),
            patch.object(
                agent.client.chat.completions,
                "create",
                side_effect=[stream, final],
            ),
        ):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    copy_context().run,
                    run_chat_stream,
                    "看看工作区",
                    emit=events.append,
                    cancel_token=token,
                )
                self.assertEqual(future.result(timeout=60), "工作区是空的")

        store = RunStore()
        with __import__("contextlib").closing(__import__("sqlite3").connect(store.db_path)) as conn:
            run_id = conn.execute(
                "SELECT id FROM agent_runs WHERE request_id = ?", (request_id,)
            ).fetchone()[0]
            execs = conn.execute("SELECT status FROM agent_execs").fetchall()

        self.assertEqual([row[0] for row in execs], ["succeeded"])

        trajectory = store.list_events(run_id)
        self.assertEqual(
            [event["event_type"] for event in trajectory],
            ["exec_started", "exec_finished"],
        )
        self.assertEqual(trajectory[-1]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
