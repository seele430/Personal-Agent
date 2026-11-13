"""tier1 runner：平台执行层创建的子进程入口。

一个 runner 只执行一次受控任务，然后退出。任务说明（JSON）从 stdin 读入，
结果写入 ``--result`` 指定的文件；stdout / stderr 留给任务本身，平台会把
两者合计到同一个输出上限。

**启动顺序是安全设计的一部分**（不要为了「少一次 IO」改掉）：

1. 宿主先创建 Job Object，并把本进程分配进去；
2. 宿主再把任务 JSON 写到 stdin；
3. 本进程读到完整 JSON 之后才允许派生任何子进程。

于是 runner 和它派生的一切进程都归属同一个 job，``TerminateJobObject``
一次即可清空整棵树。若反过来先派生再分配，跑得快的子进程会逃出 job。

runner 只做三件事：设置工作目录、按平台显式授予的路径准备导入环境、
执行一次工具或一条命令。它不读取宿主 ``.env``，也不接受任意环境变量注入。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

EXIT_OK = 0
EXIT_TOOL_ERROR = 3
EXIT_INVALID_SPEC = 4
EXIT_RUNNER_ERROR = 5

# src/platform/tier1_runner.py -> 仓库根目录
ROOT = Path(__file__).resolve().parents[2]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.platform.env_injection import (  # noqa: E402
    validate_env_injection,
)
from src.platform.trace_context import (  # noqa: E402
    TRACEPARENT_ENV,
    is_valid,
)

DATA_DIR_ENV = "PERSONAL_AGENT_DATA_DIR"
TIER1_ENV = "PERSONAL_AGENT_TIER1"


class InvalidSpec(RuntimeError):
    """宿主给的任务说明不合法；属于平台侧错误，不是工具错误。"""


class ToolCallFailed(RuntimeError):
    """工具自身抛出的异常；只包一层，保留原始类型名。"""

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tier1_runner",
        description="执行一次受控任务；任务说明来自 stdin。",
    )
    parser.add_argument("--workspace", required=True, help="本沙箱的独立工作区")
    parser.add_argument("--data-dir", default=None, help="仅工具任务需要的显式数据根")
    parser.add_argument("--result", required=True, help="结果 JSON 的落盘路径")
    parser.add_argument("--kind", required=True, choices=("tool", "command"))
    return parser.parse_args(argv)


def write_result(path: str | Path, payload: dict) -> None:
    """原子写结果文件，避免宿主读到半个 JSON。"""

    target = Path(path)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def require_mapping(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise InvalidSpec(f"{field} 必须是对象")
    return value


def require_identity(spec: dict) -> dict:
    identity = require_mapping(spec.get("identity"), "identity")

    missing = [
        name
        for name in ("user_id", "request_id", "role")
        if not isinstance(identity.get(name), str) or not identity.get(name).strip()
    ]

    if missing:
        raise InvalidSpec("身份字段缺失：" + ",".join(missing))

    return identity


def run_tool(spec: dict) -> dict:
    """在子进程里执行一次注册工具。

    身份由宿主显式传入（来自服务端认证），这里仍然走
    ``bind_authenticated_reminder``，归属校验和「没有身份就拒绝」的规则
    一行都不放宽。
    """

    tool_name = spec.get("tool")

    if not isinstance(tool_name, str) or not tool_name:
        raise InvalidSpec("tool 必须是非空字符串")

    arguments = spec.get("arguments") or {}

    if not isinstance(arguments, dict):
        raise InvalidSpec("arguments 必须是对象")

    identity = require_identity(spec)

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    # 延迟导入：只有工具任务才需要模型模块与提醒依赖。
    from src import agent
    from src.core.reminder_identity import bind_authenticated_reminder

    agent.init_db()

    definition = agent.TOOL_REGISTRY.definition(tool_name)

    if definition is None:
        raise ValueError("未知工具")

    tool_call = SimpleNamespace(
        id=str(spec.get("call_id") or "tier1-tool"),
        type="function",
        function=SimpleNamespace(
            name=tool_name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )

    with bind_authenticated_reminder(
        user_id=identity["user_id"],
        request_id=identity["request_id"],
        role=identity["role"],
    ):
        try:
            # 直接调用 handler：审批策略是宿主的决定，宿主已经把「允许执行」
            # 这一步做完才会派活下来。runner 再查一次策略只会让 ask 工具永远
            # 无法执行（旧 JSON 聊天在 D13 前依赖这一点）。
            return definition.handler(tool_call, cancel_token=None)
        except Exception as exc:  # noqa: BLE001 - 原样上报给宿主分类
            raise ToolCallFailed(exc) from exc


def run_command(spec: dict) -> int:
    """执行一条命令，返回它的退出码。

    子进程直接继承本进程的 stdout / stderr，因此平台只看到一个输出流；
    取消时宿主终止整个 job，本进程和这条命令一起结束。

    spec 可带 ``env``（白名单受控注入）：runner 独立再校验一次，
    不信任宿主的预先校验。
    """

    argv = spec.get("argv")

    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise InvalidSpec("argv 必须是非空字符串数组")

    environment = dict(os.environ)

    try:
        environment.update(validate_env_injection(spec.get("env")))
    except ValueError as exc:
        raise InvalidSpec(str(exc)) from exc

    completed = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        cwd=os.getcwd(),
        env=environment,
        check=False,
    )

    return int(completed.returncode)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    workspace = Path(args.workspace).resolve()

    if not workspace.is_dir():
        print(f"工作区不存在：{workspace}", file=sys.stderr)
        return EXIT_INVALID_SPEC

    # 工作目录先切到沙箱工作区，之后的一切相对路径都不会落到宿主仓库。
    os.chdir(workspace)

    if args.data_dir:
        # 平台显式授予的数据根。它由宿主从当前 paths 配置推导，
        # 不是从宿主进程环境继承来的，也不是凭据。
        os.environ[DATA_DIR_ENV] = str(Path(args.data_dir).resolve())

    os.environ[TIER1_ENV] = "1"

    raw = sys.stdin.read()

    try:
        spec = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        print(f"任务说明不是合法 JSON：{exc}", file=sys.stderr)
        return EXIT_INVALID_SPEC

    try:
        spec = require_mapping(spec, "spec")
    except InvalidSpec as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_INVALID_SPEC

    # D24：宿主注入的 W3C traceparent。放进本进程环境，工具 handler 与
    # 命令子进程都能继承到；非法值一律忽略（不因为坏头拒绝执行）。
    traceparent = spec.get("traceparent")

    if is_valid(traceparent):
        os.environ[TRACEPARENT_ENV] = traceparent

    if args.kind == "tool":
        try:
            result = run_tool(spec)
        except ToolCallFailed as failure:
            original = failure.original
            write_result(
                args.result,
                {
                    "ok": False,
                    "error_type": type(original).__name__,
                    "message": str(original),
                },
            )
            return EXIT_TOOL_ERROR
        except InvalidSpec as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_INVALID_SPEC
        except Exception:  # noqa: BLE001 - runner 自身故障要能被区分出来
            traceback.print_exc()
            return EXIT_RUNNER_ERROR

        write_result(args.result, {"ok": True, "result": result})
        return EXIT_OK

    try:
        return run_command(spec)
    except InvalidSpec as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_INVALID_SPEC
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return EXIT_RUNNER_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
