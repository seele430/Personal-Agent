"""真实外部 Agent 验证：HuggingFace `Agent` + MCP 工具（写产物）。

    .\\.venv/Scripts/python.exe scripts/verify_hf_agent.py --real

不加 `--real` 只打印配置、不调用模型（防止误花额度）。

与 v1.4.8 的区别：那一次是我们直接调模型 API；这一条是**外部 Agent 循环**
（模型自己决定调 MCP 工具 `write_artifact`），产物由 Agent 写、我们只做本地验收。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.collab.verify import check_artifacts, check_digest  # noqa: E402


DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"
TASK = (
    "请调用 write_artifact 工具，创建一个名为 acceptance.md 的文件，"
    "内容为一段不超过 80 字的中文说明：为什么验收要区分成果与过程。"
    "只调用一次工具，不要输出其他内容。"
)


def load_key(path: Path) -> str:
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()

            if line.startswith("HF_TOKEN="):
                return line.split("=", 1)[1].strip()

    return os.getenv("HF_TOKEN", "").strip()


async def run(model: str, api_key: str, artifact_dir: Path) -> dict:
    from huggingface_hub.inference._mcp.agent import Agent

    servers = [
        {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(ROOT / "scripts" / "mcp_artifact_server.py")],
            "env": {
                **os.environ,
                "ARTIFACT_DIR": str(artifact_dir),
            },
        }
    ]

    agent = Agent(
        model=model,
        servers=servers,
        provider="auto",
        api_key=api_key,
    )
    await agent.load_tools()
    tools = [tool.function.name for tool in agent.available_tools]

    texts = []

    async for item in agent.run(TASK):
        content = getattr(item, "content", None)

        if content:
            texts.append(str(content))

    return {"tools": tools, "output": "".join(texts)[-500:]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="真实外部 Agent（HF + MCP）")
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)

    api_key = load_key(ROOT / ".env.provider.local")

    if not args.real:
        print(f"模型：{args.model}")
        print(f"工具：MCP stdio（write_artifact）")
        print(f"HF_TOKEN：{'已配置(len=%d)' % len(api_key) if api_key else '未配置'}")

        return 2

    if not api_key:
        print("未配置 HF_TOKEN")

        return 2

    with tempfile.TemporaryDirectory() as temporary:
        artifacts = Path(temporary) / "artifacts"

        try:
            result = asyncio.run(run(args.model, api_key, artifacts))

        except Exception as error:  # noqa: BLE001 - 外部依赖失败要原样报出
            print(f"FAIL：{type(error).__name__}: {error}")

            return 1

        target = artifacts / "acceptance.md"

        if not target.is_file():
            print(json.dumps({"tools": result["tools"], "output": result["output"]}, ensure_ascii=True))
            print("FAIL：Agent 没有产出 acceptance.md")

            return 1

        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        checks = [
            check_artifacts(artifacts, ("acceptance.md",)),
            check_digest(target, digest),
        ]

        summary = {
            "model": args.model,
            "tools": result["tools"],
            "artifact_bytes": target.stat().st_size,
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks
            ],
            "wrong_digest_rejected": not check_digest(target, "0" * 64).ok,
        }
        print(json.dumps(summary, ensure_ascii=True))

        passed = all(c.ok for c in checks) and summary["wrong_digest_rejected"]
        print("PASS" if passed else "FAIL")

        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
