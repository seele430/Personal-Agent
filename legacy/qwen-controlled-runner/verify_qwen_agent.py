"""真实外部 Agent E2E：Qwen Code（headless）写产物 + 本地验收。

    .\\.venv/Scripts/python.exe scripts/verify_qwen_agent.py --real

不加 `--real` 只打印配置、不启动 agent。
凭据从 `.env` / `.env.provider.local` 读取（不打印 key）。
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

from src.collab.providers.qwen_agent import QwenAgentProvider  # noqa: E402
from src.collab.verify import check_artifacts, check_digest  # noqa: E402


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Qwen Code 真实外部 Agent 验证")
    parser.add_argument("--real", action="store_true", help="真的启动 qwen（会花 token）")
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.provider.local")

    # 本项目 .env 只存 key，不存 base_url；这里补默认的 OpenAI 兼容端点
    os.environ.setdefault("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    os.environ.setdefault("OPENAI_MODEL", "deepseek-chat")

    provider = QwenAgentProvider(timeout=args.timeout)

    if not args.real:
        print(f"qwen: {provider.qwen_path or '未找到'}")
        print(f"模型后端：{os.getenv('OPENAI_BASE_URL') or '(默认)'} / {os.getenv('OPENAI_MODEL') or '(默认)'}")
        print("不加 --real 不会启动 agent")

        return 2

    if not provider.configured:
        print("找不到 qwen：请先 npm i -g @qwen-code/qwen-code")

        return 2

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)

        try:
            result = provider.submit(
                "请调用文件写入工具，在当前目录创建文件 acceptance.md，"
                "内容为一段不超过 60 字的中文说明：为什么验收要同时看成果与过程。"
                "必须真的创建这个文件，不要只在回复里写答案。完成后输出 DONE。",
                artifact_name="acceptance.md",
                out_dir=workspace,
            )

        except Exception as error:  # noqa: BLE001 - 外部依赖失败要原样报出
            print(f"FAIL：{type(error).__name__}: {error}")

            return 1

        target = workspace / "acceptance.md"
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        checks = [
            check_artifacts(workspace, ("acceptance.md",)),
            check_digest(target, digest),
        ]

        summary = {
            "provider": result["provider"],
            "model": result["model"],
            "task_id": result["task_id"],
            "duration_s": result["duration_s"],
            "artifact_bytes": result["artifact_bytes"],
            "artifacts": result["artifacts"],
            "checks": [
                {"name": item.name, "ok": item.ok, "detail": item.detail}
                for item in checks
            ],
            "wrong_digest_rejected": not check_digest(target, "0" * 64).ok,
            "content_head": target.read_text(encoding="utf-8").strip()[:120],
        }
        print(json.dumps(summary, ensure_ascii=True))

        passed = all(item.ok for item in checks) and summary["wrong_digest_rejected"]
        print("PASS" if passed else "FAIL")

        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
