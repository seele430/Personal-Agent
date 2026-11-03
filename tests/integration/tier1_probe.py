"""D11 验收用的 tier1 探针工具（只被测试导入）。

探针不注册 Python handler：它声明一条 ``kind="command"`` 的执行规格，由平台
在 runner 子进程里派生「父进程 + 子进程」，并把两个 PID 写进测试指定的文件。
这样才能证明取消 / 超时清理的是**整棵进程树**，而不是只杀直接父进程。

handler 故意写成「被调用即失败」：如果哪天宿主又回去直接执行工具，
探针会立刻炸出来。
"""

from __future__ import annotations

import json
from pathlib import Path

PROBE_TOOL_NAME = "tier1_probe_tree"

# 父进程：写 PID、派生一个长命子进程，然后自己长眠。
SPAWN_TREE_SOURCE = "\n".join(
    [
        "import json, os, subprocess, sys, time",
        "from pathlib import Path",
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
        "Path(sys.argv[1]).write_text(",
        "    json.dumps({'parent': os.getpid(), 'child': child.pid}),",
        "    encoding='utf-8',",
        ")",
        "time.sleep(60)",
    ]
)

# 同一棵树，但写完 PID 之后狂打输出，用来触发输出上限。
SPAWN_TREE_AND_FLOOD_SOURCE = "\n".join(
    [
        "import json, os, subprocess, sys, time",
        "from pathlib import Path",
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
        "Path(sys.argv[1]).write_text(",
        "    json.dumps({'parent': os.getpid(), 'child': child.pid}),",
        "    encoding='utf-8',",
        ")",
        "for _ in range(4000):",
        "    print('x' * 200)",
        "    sys.stdout.flush()",
        "time.sleep(60)",
    ]
)


def probe_argv(python: str, pid_file, *, mode: str = "sleep") -> list[str]:
    source = (
        SPAWN_TREE_AND_FLOOD_SOURCE if mode == "flood" else SPAWN_TREE_SOURCE
    )
    return [python, "-c", source, str(pid_file)]


def read_pids(pid_file) -> dict[str, int]:
    return json.loads(Path(pid_file).read_text(encoding="utf-8"))


def wait_for_pids(pid_file, timeout: float = 5.0) -> dict[str, int]:
    """等探针把两个 PID 写进文件；写不出来就让用例直接失败。"""

    import time

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if Path(pid_file).is_file():
            try:
                return read_pids(pid_file)
            except (OSError, json.JSONDecodeError):
                pass
        time.sleep(0.02)

    raise AssertionError(f"探针没有写出 PID 文件：{pid_file}")


def _host_handler_must_not_run(tool_call, cancel_token=None):
    raise AssertionError("宿主进程不应直接执行工具 handler")


def register_probe_tool(
    python: str,
    pid_file,
    *,
    mode: str = "sleep",
    timeout_ms: int | None = None,
    max_output_bytes: int | None = None,
) -> str:
    """把一个「只走 command 规格」的探针工具注册进全局注册表。"""

    from src import agent

    execution = {
        "kind": "command",
        "argv": probe_argv(python, pid_file, mode=mode),
    }

    if timeout_ms is not None:
        execution["timeout_ms"] = timeout_ms

    if max_output_bytes is not None:
        execution["max_output_bytes"] = max_output_bytes

    agent.TOOL_REGISTRY.unregister(PROBE_TOOL_NAME)
    agent.TOOL_REGISTRY.register(
        {
            "type": "function",
            "function": {
                "name": PROBE_TOOL_NAME,
                "description": "D11 进程树探针（仅测试使用）",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        policy="allow",
        handler=_host_handler_must_not_run,
        execution=execution,
    )

    return PROBE_TOOL_NAME


def unregister_probe_tool() -> None:
    from src import agent

    agent.TOOL_REGISTRY.unregister(PROBE_TOOL_NAME)
