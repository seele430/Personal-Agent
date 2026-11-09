"""通知出站真实送达验证（ntfy）。

    .\\.venv/Scripts/python.exe scripts/verify_notify.py --real

不加 `--real` 只打印配置、不发送。
topic 只显示前 4 位，避免把完整主题打进日志。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import sys
import time

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return

    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")

        if name and name not in os.environ:
            os.environ[name] = value


def mask(topic: str) -> str:
    return f"{topic[:4]}***（len={len(topic)}）"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ntfy 通知出站验证")
    parser.add_argument("--real", action="store_true", help="真的发一条推送")
    parser.add_argument("--keep-topic", action="store_true", help="复用环境里的 topic，不新建")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.provider.local")

    topic = (os.getenv("NTFY_TOPIC") or "").strip()

    if not topic and not args.keep_topic:
        alphabet = string.ascii_lowercase + string.digits
        topic = "pa-" + "".join(secrets.choice(alphabet) for _ in range(29))

    server = (os.getenv("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")

    if not args.real:
        print(f"server: {server}")
        print(f"topic: {mask(topic) if topic else '未配置'}")
        print("不加 --real 不会发送")

        return 2

    if not topic:
        print("未配置 NTFY_TOPIC")

        return 2

    # 随机生成的 topic 要写回环境，send_notification 才读得到
    os.environ["NTFY_TOPIC"] = topic
    os.environ.setdefault("NTFY_SERVER", server)

    import requests

    message = f"Personal Agent 通知测试 {time.strftime('%Y-%m-%d %H:%M:%S')}"
    started = time.perf_counter()

    response = requests.post(
        f"{server}/{topic}",
        data=message.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8"},
        timeout=15,
    )
    elapsed = round(time.perf_counter() - started, 2)

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    message_id = str(payload.get("id") or "")

    # 同时验证项目里真正被调用的函数
    from src.notification import send_notification

    module_result = send_notification(message + "（module path）")

    summary = {
        "server": server,
        "topic": mask(topic),
        "http_status": response.status_code,
        "message_id": message_id,
        "module_result": module_result,
        "duration_s": elapsed,
    }
    print(json.dumps(summary, ensure_ascii=True))

    passed = response.status_code == 200 and bool(message_id) and module_result is True
    print("PASS" if passed else "FAIL")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
