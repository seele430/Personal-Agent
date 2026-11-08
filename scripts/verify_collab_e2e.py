"""协作链路端到端验证：真实 HTTP provider → 登记 → 轮询 → 产物 → 本地验收。

    python scripts/verify_collab_e2e.py

要点：验收只认**本地**证据（文件存在 + 本地算的 sha256）；
provider 自述的"成功"一律不参与判定——脚本最后还会故意把哈希写错，
断言 verify 必须判 False（证明这条底线没被绕过）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
import urllib.request

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.collab.registry import ExternalTaskRegistry  # noqa: E402
from src.collab.verify import check_artifacts, check_digest  # noqa: E402


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read()


def main() -> int:
    process = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "scripts" / "mock_provider.py"), "--port", "0", "--delay", "2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    try:
        port = None

        for _ in range(50):
            line = process.stdout.readline() if process.stdout else ""

            if line.startswith("PORT="):
                port = int(line.strip().split("=", 1)[1])
                break

        if port is None:
            print("FAIL：没拿到 mock provider 端口")

            return 1

        base = f"http://127.0.0.1:{port}"
        submitted = post_json(f"{base}/submit", {"prompt": "写一份周报"})
        task_id = submitted["task_id"]
        print(f"submit -> task_id={task_id}")

        with tempfile.TemporaryDirectory() as temporary:
            registry = ExternalTaskRegistry(Path(temporary) / "tasks.db")
            registry.register("mock", task_id, run_id="run-1", state="running")
            print(f"registry: {registry.get('mock', task_id).state}")

            deadline = time.time() + 30
            artifacts: list[str] = []

            while time.time() < deadline:
                status = get_json(f"{base}/status/{task_id}")

                if status.get("state") == "succeeded":
                    artifacts = status.get("artifacts") or []
                    break

                time.sleep(0.5)

            if not artifacts:
                print("FAIL：provider 一直没给产物")

                return 1

            registry.update_state("mock", task_id, "succeeded")
            artifact_dir = Path(temporary) / "artifacts"
            artifact_dir.mkdir()

            for name in artifacts:
                (artifact_dir / name).write_bytes(get_bytes(f"{base}/artifact/{name}"))

            first = artifact_dir / artifacts[0]
            digest = hashlib.sha256(first.read_bytes()).hexdigest()

            checks = [
                check_artifacts(artifact_dir, tuple(artifacts)),
                check_digest(first, digest),
            ]
            # 反例：哈希故意写错，必须判 False
            wrong = check_digest(first, "0" * 64)

            payload = {
                "task_id": task_id,
                "registry_state": registry.get("mock", task_id).state,
                "artifacts": artifacts,
                "checks": [
                    {"name": item.name, "ok": item.ok, "detail": item.detail}
                    for item in checks
                ],
                "wrong_digest_rejected": not wrong.ok,
            }
            print(json.dumps(payload, ensure_ascii=True))

            passed = all(item.ok for item in checks) and payload["wrong_digest_rejected"]
            print("PASS" if passed else "FAIL")

            return 0 if passed else 1

    finally:
        process.terminate()

        try:
            process.wait(timeout=5)

        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
