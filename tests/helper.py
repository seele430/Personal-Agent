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

from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from src import config


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
