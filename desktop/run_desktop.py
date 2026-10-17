"""Personal Agent 桌面版入口。

一个进程里同时跑：
  - api（uvicorn，线程）
  - scheduler（线程）
  - outbox（线程）
  - pywebview 窗口（主线程，必须）

并处理三件打包特有的事：
  1. 数据目录改成 %APPDATA%\\PersonalAgent（装到 Program Files 也能写）；
  2. edge_tts 的子进程调用：打包后 sys.executable 是 exe 自己，
     所以要在入口拦住 `-m edge_tts` 这种参数，转去跑 CLI；
  3. 前端由后端托管（见 src/main.py 的 _mount_frontend）。

开发模式也能直接跑：python desktop/run_desktop.py
"""

import os
import sys
import threading
import time

APP_NAME = "PersonalAgent"
HOST = "127.0.0.1"
PORT = 8000


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _data_dir() -> str:
    """打包版写 %APPDATA%，开发版仍然用项目根（和 run-dev 一致）。"""
    if _is_frozen():
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, APP_NAME)
    else:
        path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    os.makedirs(path, exist_ok=True)

    return path


ENV_TEMPLATE = """# Personal Agent 配置
# 填入你自己的 DeepSeek（或任意 OpenAI 兼容）API Key 后重启本程序。

OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.deepseek.com
MODEL=deepseek-chat

# 可选：通知推送
NTFY_SERVER=
NTFY_TOPIC=
"""


def _prepare_env() -> None:
    data = _data_dir()

    os.environ.setdefault("PERSONAL_AGENT_DATA_DIR", data)
    os.chdir(data)

    # 打包版没有控制台，把运行日志写进数据目录，
    # 出问题（比如「无法创建房间」）时能直接看到 traceback。
    import logging

    logging.basicConfig(
        filename=os.path.join(data, "app.log"),
        filemode="a",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
    )

    env_path = os.path.join(data, ".env")

    # 首次启动：生成模板，让用户自己填 key（不把开发者的 key 打进安装包）
    if not os.path.exists(env_path):
        with open(env_path, "w", encoding="utf-8") as handle:
            handle.write(ENV_TEMPLATE)

    # 再从数据目录加载：src/agent.py 自己的 load_dotenv 在打包后
    # 会指向 _internal/.env（不存在），所以这里先注入环境变量。
    try:
        from dotenv import load_dotenv  # noqa: PLC0415

        load_dotenv(env_path, override=False)
    except Exception:  # noqa: BLE001
        pass


def _run_edge_tts_cli() -> bool:
    """打包后拦截 `exe -m edge_tts ...`：转成真正的 CLI 调用。

    src/room_api.py 里用 `[sys.executable, "-m", "edge_tts", ...]` 起子进程，
    打包后 sys.executable 指向 PersonalAgent.exe 自己，不加这个拦截就会
    无限递归/直接失败。
    """

    argv = sys.argv

    if len(argv) >= 4 and argv[1] == "-m" and argv[2] == "edge_tts":
        sys.argv = [argv[0], *argv[3:]]

        import edge_tts.__main__ as cli  # noqa: PLC0415

        cli.main()

        return True

    return False


def _start_api() -> "object":
    import uvicorn  # noqa: PLC0415

    from src.main import app  # noqa: PLC0415

    config = uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        log_level="warning",
        access_log=False,
        # 打包后 uvicorn 的 LOGGING_CONFIG 里引用的 formatter 在冻结环境
        # 解析不到（ValueError: Unable to configure formatter 'default'），
        # 干脆让它别接管日志，用 logging 默认配置即可。
        log_config=None,
    )

    server = uvicorn.Server(config)

    threading.Thread(target=server.run, daemon=True).start()

    # 等 API 真正能响应，再开窗口
    import urllib.request  # noqa: PLC0415

    deadline = time.time() + 30

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://{HOST}:{PORT}/health", timeout=2
            ) as resp:
                if resp.status == 200:
                    return server
        except Exception:  # noqa: BLE001 - 还没起来就继续等
            time.sleep(0.2)

    return server


def _start_workers() -> None:
    """把原来两个独立进程塞进线程。它们内部都是 while True 循环。"""

    try:
        from src import scheduler  # noqa: PLC0415

        threading.Thread(target=scheduler.main, daemon=True).start()
    except Exception:  # noqa: BLE001 - 缺配置时不该挡住窗口
        pass

    try:
        from src.core import outbox_service  # noqa: PLC0415

        threading.Thread(target=outbox_service.main, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def _log(msg: str) -> None:
    """打包版没有控制台，所以把关键步骤写进数据目录的 boot.log。"""
    try:
        path = os.path.join(_data_dir(), "boot.log")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:  # noqa: BLE001 - 日志失败不能影响启动
        pass


def main() -> None:
    if _run_edge_tts_cli():
        return

    _log("boot start")

    try:
        _prepare_env()
        _log(f"env ready, data={_data_dir()}")
    except Exception as error:  # noqa: BLE001
        _log(f"prepare_env FAILED: {error!r}")
        raise

    try:
        _start_api()
        _log("api started")
    except Exception as error:  # noqa: BLE001
        _log(f"api FAILED: {error!r}")
        raise

    try:
        _start_workers()
        _log("workers started")
    except Exception as error:  # noqa: BLE001
        _log(f"workers FAILED: {error!r}")

    import webview  # noqa: PLC0415

    _log("creating window")

    webview.create_window(
        "Personal Agent",
        f"http://{HOST}:{PORT}",
        width=1320,
        height=860,
        min_size=(980, 640),
    )

    webview.start()

    _log("window closed, exiting")

    # 窗口关掉就结束整个进程（后台线程是 daemon，会跟着退）
    os._exit(0)


if __name__ == "__main__":
    main()
