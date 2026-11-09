"""最小 MCP stdio server：给外部 Agent 一个"写产物"的工具。

工具：write_artifact(name, content)
产物只写到环境变量 ARTIFACT_DIR 指定的目录（默认 ./artifacts），
文件名白名单：字母数字与 - _ .，拒绝路径分隔符。
"""

from __future__ import annotations

import os
import re

from pathlib import Path

from mcp.server.fastmcp import FastMCP


NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
mcp = FastMCP("artifact-writer")


def artifact_dir() -> Path:
    return Path(os.environ.get("ARTIFACT_DIR") or "artifacts").resolve()


@mcp.tool()
def write_artifact(name: str, content: str) -> str:
    """把一段文本写成产物文件，返回绝对路径。"""

    if not NAME_PATTERN.match(str(name or "").strip()):
        raise ValueError("name 只允许字母数字与 - _ .，且不能含路径")

    if not str(content or "").strip():
        raise ValueError("content 不能为空")

    target = artifact_dir() / name.strip()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    return str(target)


if __name__ == "__main__":
    mcp.run()
