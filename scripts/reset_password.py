"""重置某个账号的密码（本地版本没有邮箱找回，只能从可信终端操作）。

用法（数据目录要与启动服务时一致）：

    $env:PERSONAL_AGENT_DATA_DIR = "$env:TEMP\pa-demo"
    .\\venv\\Scripts\\python.exe scripts\\reset_password.py --username seele

密码要求 12—1024 字节；成功后会吊销该用户所有旧会话。
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
    parser = argparse.ArgumentParser(description="重置账号密码")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", default=None, help="不传则提示输入")
    parser.add_argument(
        "--password-env",
        default=None,
        help="从环境变量读密码（避免留在命令历史里）",
    )
    parser.add_argument("--data-dir", default=None)
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
        if not sys.stdin.isatty():
            print("(非交互终端：密码将明文回显)", file=sys.stderr)
            password = input("new password (12+ bytes): ")
        else:
            password = getpass.getpass("new password (12+ bytes): ")
            confirm = getpass.getpass("repeat: ")

            if password != confirm:
                print("两次输入不一致", file=sys.stderr)
                return 2

    store = AuthStore(config.paths.auth_users_db)
    store.init_db()

    try:
        user = store.set_password(args.username, password)
    except AuthError as exc:
        print(f"重置失败：{exc}", file=sys.stderr)

        if "字节" in str(exc):
            print(
                f"当前密码 {len(password.encode('utf-8'))} 字节；需要至少 12 字节",
                file=sys.stderr,
            )

        return 1

    print(
        f"密码已重置：{user['username']}（旧会话已全部吊销）\n"
        f"数据库：{config.paths.auth_users_db}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
