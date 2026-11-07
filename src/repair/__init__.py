"""修复闭环（W5）：固定 bug 夹具 + 有界修复循环。"""

from src.repair.fixtures import (
    FIXTURES,
    BugFixture,
    get_fixture,
    list_fixtures,
)
from src.repair.loop import (
    MAX_ROUNDS,
    RepairError,
    RepairLoop,
    RepairResult,
    RunResult,
    default_runner,
    parse_failures,
    run_fixture,
)


__all__ = [
    "FIXTURES",
    "BugFixture",
    "MAX_ROUNDS",
    "RepairError",
    "RepairLoop",
    "RepairResult",
    "RunResult",
    "default_runner",
    "get_fixture",
    "list_fixtures",
    "parse_failures",
    "run_fixture",
]
