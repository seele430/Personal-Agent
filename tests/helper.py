"""测试用数据库路径隔离。

背景：src/config.py 提供全局 paths 对象，但各模块用的是
`from src.config import paths` —— 这是一个早绑定：
只 patch("src.config.paths", tmp) 的话，已经导入的模块仍然
持有旧对象，于是继续打开项目根目录下的真实数据库。
这正是受保护运行器里 "Test cannot access project database" 的来源。

isolate_paths() 一次替换三处：
1. src.config.paths（之后的导入会拿到新对象）
2. 所有已导入模块里早绑定的那个 paths 名字
3. reminder_identity.REMINDER_DB_PATH 这类导入期快照

用法（setUp 内一行，清理自动注册）：

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

也可以当上下文管理器用：

    with isolated_paths(self.temp):
        ...
"""

import sys
import time

from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from src import config


def wait_for_exec_worker(exec_id: str, timeout: float = 30.0) -> None:
    """等后台 exec worker 真正退出，再让测试拆临时目录。

    Windows 上临时目录里还有打开着的 SQLite 连接时，rmtree 会报
    ``WinError 32``（另一个程序正在使用此文件）。HTTP 层在写终态之后才把
    exec 从活动表里摘掉，所以「状态变 cancelled」不等于「worker 已收尾」。
    """

    from src.platform import active_execs

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if exec_id not in active_execs.snapshot():
            return
        time.sleep(0.01)

    raise AssertionError(f"exec {exec_id} 的 worker 在 {timeout}s 内没有退出")


def build_temp_paths(container) -> config.Paths:
    """按临时目录构造一套路径。

    container 可以是 TemporaryDirectory 对象或任意路径。
    """

    root = Path(getattr(container, "name", container))

    return config.build_paths(root)


def _enter(stack: ExitStack, container) -> config.Paths:
    temporary = build_temp_paths(container)
    original = config.paths

    stack.enter_context(patch.object(config, "paths", temporary))

    # 早绑定修正：模块级 `from src.config import paths`
    for module in list(sys.modules.values()):
        if getattr(module, "paths", None) is original:
            stack.enter_context(patch.object(module, "paths", temporary))

    # 导入期快照修正：reminder_identity.REMINDER_DB_PATH
    identity = sys.modules.get("src.core.reminder_identity")

    if identity is not None and hasattr(identity, "REMINDER_DB_PATH"):
        stack.enter_context(
            patch.object(
                identity,
                "REMINDER_DB_PATH",
                temporary.reminders_db,
            )
        )

    # D26：默认关闭 /v1 限流 —— 否则同进程的所有用例共享一个令牌桶，
    # 跑着跑着就 429。限流用例自己设值并 reset_limiter()。
    stack.enter_context(
        patch.dict(
            "os.environ",
            {"PERSONAL_AGENT_PLATFORM_RATE_PER_MIN": "0"},
            clear=False,
        )
    )

    limiter_module = sys.modules.get("src.platform.rate_limit")

    if limiter_module is not None:
        stack.enter_context(
            patch.object(limiter_module, "_limiter", None)
        )

    return temporary


def isolate_paths(case, container) -> config.Paths:
    """给 unittest 用例装上临时数据库路径，用例结束时自动还原。"""

    stack = ExitStack()
    case.addCleanup(stack.close)

    return _enter(stack, container)


@contextmanager
def isolated_paths(container):
    """上下文管理器版本。"""

    with ExitStack() as stack:
        yield _enter(stack, container)


def tool_result_outcome(result: dict | None = None):
    """平台执行成功的替身结果。

    D11 起工具在 tier1 子进程里执行，宿主不再调用 ``agent.create_reminder``
    这类 handler。需要「工具已经执行成功」的用例改为替身平台执行层。
    """

    from src.platform.exec_service import ExecOutcome

    return ExecOutcome(
        sandbox_id="sbx_test",
        status="succeeded",
        exit_code=0,
        stdout="",
        stderr="",
        truncated=False,
        result=result if result is not None else {"success": True},
        error_code=None,
    )


def tool_error_outcome(error_type: str, message: str):
    """平台执行完成、但工具自身报错的替身结果。"""

    from src.platform.exec_service import ExecOutcome

    return ExecOutcome(
        sandbox_id="sbx_test",
        status="failed",
        exit_code=3,
        stdout="",
        stderr="",
        truncated=False,
        tool_error={"ok": False, "error_type": error_type, "message": message},
        error_code="TOOL_FAILED",
    )


def called_tool(mock):
    """取出交给平台执行层的 (工具名, 参数字典)。"""

    import json

    call = mock.call_args

    if call is None:
        return None

    tool_call = call.args[0] if call.args else call.kwargs["tool_call"]

    return (
        tool_call.function.name,
        json.loads(tool_call.function.arguments),
    )
