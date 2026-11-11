"""只读探测 Caddy：路径、版本、端口占用。不安装、不启动。"""

import json
import shutil
import subprocess
import sys

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    caddy = shutil.which("caddy")
    candidates = [
        Path(r"C:\Program Files\Caddy\caddy.exe"),
        Path(r"C:\ProgramData\chocolatey\bin\caddy.exe"),
    ]
    path = Path(caddy) if caddy else next((item for item in candidates if item.is_file()), None)
    version = None

    if path is not None:
        try:
            completed = subprocess.run(
                [str(path), "version"], capture_output=True, text=True, timeout=10
            )
            version = (completed.stdout or "").strip() or None
        except (OSError, subprocess.SubprocessError):
            version = None

    ports = {}

    for port in (18443, 18000, 2019):
        try:
            import socket

            with socket.socket() as sock:
                sock.bind(("127.0.0.1", port))
                ports[port] = "free"
        except OSError:
            ports[port] = "busy"

    print(json.dumps({
        "caddy_available": path is not None,
        "caddy_path": str(path) if path else None,
        "caddy_version": version,
        "ports": ports,
    }, ensure_ascii=False, indent=2))

    return 0 if path is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
