"""真实 Provider E2E：一次真实网络调用 + 本地验收。

    .\\.venv/Scripts/python.exe scripts/verify_real_provider.py --real

不带 `--real` 只打印说明、不发请求（防止误烧 token）。
凭据从 `.env` 读取（`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL`），不打印密钥。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.collab.providers import ProviderError, build_provider  # noqa: E402
from src.collab.registry import ExternalTaskRegistry  # noqa: E402
from src.collab.verify import check_artifacts, check_digest  # noqa: E402


def load_dotenv(path: Path) -> None:
    """极简 .env 读取：只补未设置的键，不覆盖已有环境变量。"""

    if not path.is_file():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")

        if name and name not in os.environ:
            os.environ[name] = value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="真实 Provider 端到端验证")
    parser.add_argument("--real", action="store_true", help="真的发请求（会产生 token 费用）")
    parser.add_argument(
        "--prompt",
        default="用不超过 120 字说明：为什么验收要区分「成果」与「过程」。",
    )
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.provider.local")

    if not args.real:
        print("这是真实网络调用，会产生少量 token 费用。确认要跑请加 --real")
        print(f"模型：{os.getenv('MODEL') or 'deepseek-flash'}；端点：{os.getenv('OPENAI_BASE_URL') or 'https://api.deepseek.com'}")

        return 2

    provider = build_provider()

    if not provider.configured:
        print("未配置 OPENAI_API_KEY，无法验证")

        return 2

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        artifacts = workspace / "artifacts"

        try:
            result = provider.submit(
                args.prompt, artifact_name="report.md", out_dir=artifacts
            )

        except ProviderError as error:
            print(f"FAIL：{error}")

            return 1

        registry = ExternalTaskRegistry(workspace / "tasks.db")
        registry.register(
            provider.name,
            result["task_id"],
            run_id="real-1",
            state="running",
            artifacts=("report.md",),
        )
        registry.update_state(provider.name, result["task_id"], "succeeded")

        target = artifacts / "report.md"
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        checks = [
            check_artifacts(artifacts, ("report.md",)),
            check_digest(target, digest),
        ]
        wrong = check_digest(target, "0" * 64)

        summary = {
            "provider": result["provider"],
            "model": result["model"],
            "task_id": result["task_id"],
            "duration_s": result["duration_s"],
            "usage": result["usage"],
            "artifact_bytes": target.stat().st_size,
            "checks": [
                {"name": item.name, "ok": item.ok, "detail": item.detail}
                for item in checks
            ],
            "wrong_digest_rejected": not wrong.ok,
            "registry_state": registry.get(provider.name, result["task_id"]).state,
        }
        print(json.dumps(summary, ensure_ascii=True))

        passed = all(item.ok for item in checks) and summary["wrong_digest_rejected"]
        print("PASS" if passed else "FAIL")

        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
