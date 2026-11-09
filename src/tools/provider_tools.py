"""`external_task` 工具：调用真实外部模型 API 生成文本产物。

**它不是"外部 Agent"**：没有任务状态机、没有工具调用、没有远端产物目录。
它做的事只有一件：把 prompt 发给一个 OpenAI 兼容端点，把正文写进工作区。

安全约束（写死）：

- `policy = ask`：会联网、会花 token，默认要人工确认；
- 产物只能写 `<cwd>/external-artifacts/`，文件名白名单（字母数字、`-_.`），
  拒绝路径分隔符、`..`、盘符与空名；
- 错误一律中文短句，不拼接密钥。
"""

from __future__ import annotations

import json
import re

from pathlib import Path
from typing import Any

from src.collab.providers import build_provider, ProviderError


NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_ARTIFACT = "external-report.md"
ARTIFACT_DIR = "external-artifacts"


EXTERNAL_TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "external_task",
        "description": (
            "调用外部服务生成文本产物：默认是模型 API（一次补全）；"
            "配置成 agent 模式时会真的创建文件、耗时可达数十秒，"
            "此时 prompt 必须写清要创建的文件名。会产生费用，需要用户确认。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "要生成的正文要求，尽量具体（字数、结构、语言）",
                },
                "artifact_name": {
                    "type": "string",
                    "description": (
                        "产物文件名，默认 external-report.md；只允许字母数字与 - _ .。"
                        "agent 模式下也要在 prompt 里写明这个文件名。"
                    ),
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
}


def _arguments(tool_call) -> dict:
    raw = getattr(tool_call.function, "arguments", "") or "{}"

    if isinstance(raw, dict):
        return raw

    try:
        parsed = json.loads(raw)

    except json.JSONDecodeError:
        raise ValueError("工具参数不是合法 JSON") from None

    if not isinstance(parsed, dict):
        raise ValueError("工具参数必须是对象")

    return parsed


def safe_artifact_name(raw: Any) -> str:
    text = str(raw or "").strip() or DEFAULT_ARTIFACT

    if not NAME_PATTERN.match(text):
        raise ValueError("artifact_name 只允许字母数字与 - _ .，且不能含路径")

    return text


def external_task(tool_call, cancel_token=None) -> dict[str, Any]:
    args = _arguments(tool_call)
    prompt = str(args.get("prompt") or "").strip()

    if not prompt:
        raise ValueError("prompt 不能为空")

    artifact_name = safe_artifact_name(args.get("artifact_name"))
    provider = build_provider()

    if not provider.configured:
        raise RuntimeError("未配置 OPENAI_API_KEY：无法调用外部模型 API")

    if cancel_token is not None:
        cancel_token.check()

    out_dir = Path.cwd() / ARTIFACT_DIR

    try:
        result = provider.submit(
            prompt, artifact_name=artifact_name, out_dir=out_dir
        )

    except ProviderError as error:
        message = str(error)

        if "没有产出" in message:
            message += "（agent 模式下请在 prompt 里写清要创建的文件名）"

        raise RuntimeError(message) from None

    return {
        "success": True,
        "path": f"{ARTIFACT_DIR}/{artifact_name}",
        "task_id": result["task_id"],
        "model": result["model"],
        "duration_s": result["duration_s"],
        "usage": result["usage"],
    }


TOOL_DEFINITIONS = (
    (EXTERNAL_TASK_TOOL, "ask", external_task),
)
