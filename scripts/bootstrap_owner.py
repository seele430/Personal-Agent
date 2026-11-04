r"""首次运行：创建站长账号。

产品目前没有注册入口（`bootstrap_owner` 只允许在空用户库上、从本地终端执行），
所以首次使用需要跑一次这个脚本。密码要求 12—1024 字节。

用法：

    $env:PERSONAL_AGENT_DATA_DIR = "$env:TEMP\pa-demo"   # 与启动服务时一致
    .\\venv\\Scripts\\python.exe scripts\\bootstrap_owner.py --username seele

不带 --password 时会用隐藏输入提示你输入两次。
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="创建站长账号（空用户库）")
    parser.add_argument("--username", required=True, help="3—32 位，小写字母开头")
    parser.add_argument("--password", default=None, help="不传则用隐藏输入")
    parser.add_argument(
        "--password-env",
        default=None,
        help="从该环境变量读密码（推荐：避免出现在命令行历史里）",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="数据目录；默认读 PERSONAL_AGENT_DATA_DIR，再默认项目根",
    )
    args = parser.parse_args()

    from src import config

    if args.data_dir:
        config.paths = config.build_paths(args.data_dir)

    from src.core.auth_store import AuthError, AuthStore

    password = args.password

    if args.password_env:
        password = os.environ.get(args.password_env, "")

        if not password:
            print(f"环境变量 {args.password_env} 为空", file=sys.stderr)
            return 2

    if password is None:
        # VS Code 的集成终端没有可用的控制台句柄，getpass 会直接卡住；
        # 非 tty 时退化成明文输入（并提示），保证不会挂死。
        if not sys.stdin.isatty():
            print("(非交互终端：密码将明文回显)", file=sys.stderr)
            password = input("password (12+ bytes): ")
        else:
            password = getpass.getpass("password (12+ bytes): ")
            confirm = getpass.getpass("repeat: ")

            if password != confirm:
                print("两次输入不一致", file=sys.stderr)
                return 2

    store = AuthStore(config.paths.auth_users_db)
    store.init_db()

    try:
        user = store.bootstrap_owner(args.username, password)
    except AuthError as exc:
        print(f"创建失败：{exc}", file=sys.stderr)
        return 1

    print(f"created owner {user['username']} in {config.paths.auth_users_db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
