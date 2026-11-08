"""模拟外部 Provider（stdlib http.server），用于验证协作链路。

    python scripts/mock_provider.py --port 0 --delay 3

协议（最小集）：

    POST /submit          {"prompt": "..."}        -> {"task_id": "..."}
    GET  /status/<id>                              -> {"state": "running"|"succeeded", "artifacts": [...]}
    GET  /artifact/<name>                          -> 文本产物

`--port 0` 时自动选端口，并在 stdout 打印 `PORT=<n>`，供启动脚本解析。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


TASKS: dict[str, dict] = {}
LOCK = threading.Lock()
DELAY = 3.0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 静音
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/submit":
            self._json(404, {"error": "not found"})

            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"

        try:
            payload = json.loads(raw.decode("utf-8") or "{}")

        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})

            return

        task_id = uuid.uuid4().hex[:12]

        with LOCK:
            TASKS[task_id] = {
                "created": time.monotonic(),
                "prompt": str(payload.get("prompt") or ""),
                "artifact": f"report-{task_id}.md",
            }

        self._json(200, {"task_id": task_id})

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/status/"):
            task_id = self.path.rsplit("/", 1)[-1]

            with LOCK:
                task = TASKS.get(task_id)

            if task is None:
                self._json(404, {"error": "unknown task"})

                return

            done = (time.monotonic() - task["created"]) >= DELAY
            self._json(
                200,
                {
                    "task_id": task_id,
                    "state": "succeeded" if done else "running",
                    "artifacts": [task["artifact"]] if done else [],
                },
            )

            return

        if self.path.startswith("/artifact/"):
            name = self.path.rsplit("/", 1)[-1]

            with LOCK:
                task = next(
                    (item for item in TASKS.values() if item["artifact"] == name), None
                )

            if task is None:
                self._json(404, {"error": "unknown artifact"})

                return

            self._text(200, f"# 模拟产物 {name}\n\nprompt: {task['prompt']}\n")

            return

        self._json(404, {"error": "not found"})


def serve(port: int = 0, delay: float = 3.0) -> ThreadingHTTPServer:
    global DELAY
    DELAY = delay

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="模拟外部 Provider")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--delay", type=float, default=3.0)
    args = parser.parse_args(argv)

    server = serve(args.port, args.delay)
    print(f"PORT={server.server_address[1]}", flush=True)

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        pass

    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
