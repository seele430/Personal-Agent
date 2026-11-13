"""沙箱内执行包装：经 COMSPEC /c 启动 dsh.cmd，事件落盘绕开输出上限。

为什么需要它：

- Windows 上 `.cmd` 不能被 CreateProcess 直接执行，必须经 cmd.exe；
- `cmd /c "带空格路径" args` 会触发 cmd 的「恰好两个引号则剥引号」规则，
  导致路径被拆坏。规避法：整条命令再套一层引号（见 _cmd_line）。
- dsh 的流式事件体积可超过平台 1 MiB 输出上限，stdout/stderr 由本脚本
  直接落文件（dsh.events.jsonl / dsh.err.txt），平台输出只约束本脚本
  自身的 print（正常为空）。

只依赖 stdlib。退出码 = dsh 子进程退出码；spawn 失败返回 2。

用法：
    python dsh_exec_wrapper.py --dsh <dsh.cmd 路径> --cwd <工作目录> \
        --stdin-file <提示词文件> --stdout-file <事件输出> \
        --stderr-file <错误输出> -- <dsh 参数...>
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys


def _quote(arg: str) -> str:
    if not arg or any(ch in arg for ch in ' \t"&'):
        return '"' + arg.replace('"', '\\"') + '"'

    return arg


def _cmd_line(dsh: str, args: list[str]) -> str:
    # 只构造内层命令；外层的 /c 引号由 main 里的完整命令字符串加。
    return " ".join([_quote(dsh), *(_quote(a) for a in args)])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dsh headless 沙箱执行包装")
    parser.add_argument("--dsh", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--stdin-file", required=True)
    parser.add_argument("--stdout-file", required=True)
    parser.add_argument("--stderr-file", required=True)
    parser.add_argument("dsh_args", nargs=argparse.REMAINDER)

    ns = parser.parse_args(argv)
    dsh_args = ns.dsh_args

    if dsh_args and dsh_args[0] == "--":
        dsh_args = dsh_args[1:]

    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    # 注意：必须以单个字符串传给 CreateProcess。若用 argv 列表，Python 的
    # list2cmdline 会用反斜杠转义内层引号，而 cmd.exe 不认这种转义。
    command = f'{_quote(comspec)} /c "{_cmd_line(ns.dsh, dsh_args)}"'

    try:
        with (
            open(ns.stdin_file, "rb") as stdin,
            open(ns.stdout_file, "wb") as stdout,
            open(ns.stderr_file, "wb") as stderr,
        ):
            completed = subprocess.run(
                command,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                cwd=ns.cwd,
                check=False,
            )
    except OSError as exc:
        print(f"spawn_failed: {exc}", file=sys.stderr)
        return 2

    return int(completed.returncode)


if __name__ == "__main__":
    sys.exit(main())
