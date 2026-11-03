"""tier1 沙箱工具的 handler：read / write / ls / shell / python / http。

这些 handler **只在 runner 子进程里执行**（见 ``src/platform/tier1_runner.py``），
进程的工作目录就是平台分配的沙箱工作区。宿主进程直接调用它们是错误的。

路径规则（D12）：

- 只接受工作区内的相对路径；
- 先解析 real path（含符号链接），再判断是否仍位于工作区内；
- 绝对路径、盘符相对路径、``..`` 穿越、指向工作区外的符号链接一律拒绝；
- 不用字符串前缀判断归属 —— ``C:/ws-other`` 不能被 ``C:/ws`` 放行。

tier1 不是安全沙箱：这里防的是「工具把路径写错」，不是恶意代码逃逸。
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request

from pathlib import Path
from urllib.parse import urlsplit

MAX_READ_BYTES = 65_536
# 与 agent.MAX_TOOL_ARGUMENT_BYTES 对齐：参数本身先受 32 KB 限制。
MAX_WRITE_BYTES = 32_768
MAX_LIST_ENTRIES = 200
MAX_HTTP_BYTES = 65_536
HTTP_TIMEOUT_SECONDS = 5
MAX_PATH_LENGTH = 1024


def workspace_root() -> Path:
    """runner 的工作目录就是本沙箱的工作区。"""

    return Path.cwd().resolve()


def _relative(path: Path) -> str:
    root = workspace_root()

    if path == root:
        return "."

    return path.relative_to(root).as_posix()


def _ensure_inside(path: Path) -> Path:
    root = workspace_root()

    if path != root and root not in path.parents:
        raise ValueError("路径越界：不在当前沙箱工作区内")

    return path


def resolve_in_workspace(raw) -> Path:
    """把工具参数里的路径解析成工作区内的真实路径。"""

    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("路径不能为空")

    if len(raw) > MAX_PATH_LENGTH:
        raise ValueError("路径太长")

    if "\x00" in raw:
        raise ValueError("路径不能包含空字节")

    candidate = Path(raw)

    if candidate.drive or candidate.root or candidate.is_absolute():
        raise ValueError("只接受工作区内的相对路径")

    return _ensure_inside((workspace_root() / candidate).resolve())


def _arguments(tool_call) -> dict:
    raw = getattr(tool_call.function, "arguments", "{}") or "{}"
    arguments = json.loads(raw)

    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是对象")

    return arguments


def _text(data: bytes | str) -> str:
    if isinstance(data, str):
        return data[:MAX_READ_BYTES]

    return data[:MAX_READ_BYTES].decode("utf-8", "replace")


# ==========================================
# 文件工具
# ==========================================

def read_file(tool_call, cancel_token=None) -> dict:
    args = _arguments(tool_call)
    path = resolve_in_workspace(args.get("path"))

    if not path.is_file():
        raise ValueError("文件不存在")

    data = path.read_bytes()
    truncated = len(data) > MAX_READ_BYTES

    return {
        "success": True,
        "path": _relative(path),
        "content": _text(data),
        "truncated": truncated,
        "size_bytes": len(data),
    }


def write_file(tool_call, cancel_token=None) -> dict:
    args = _arguments(tool_call)
    path = resolve_in_workspace(args.get("path"))

    content = args.get("content")

    if not isinstance(content, str):
        raise ValueError("content 必须是字符串")

    data = content.encode("utf-8")

    if len(data) > MAX_WRITE_BYTES:
        raise ValueError("写入内容超过上限")

    path.parent.mkdir(parents=True, exist_ok=True)

    # mkdir 之后再解析一次：防止中间目录是符号链接，把写入带出工作区。
    final = _ensure_inside(path.resolve())

    if final.exists() and not final.is_file():
        raise ValueError("目标不是普通文件")

    final.write_bytes(data)

    return {
        "success": True,
        "path": _relative(final),
        "bytes_written": len(data),
    }


def list_dir(tool_call, cancel_token=None) -> dict:
    args = _arguments(tool_call)
    path = resolve_in_workspace(args.get("path") or ".")

    if not path.is_dir():
        raise ValueError("目录不存在")

    items = sorted(path.iterdir(), key=lambda item: item.name)
    truncated = len(items) > MAX_LIST_ENTRIES
    entries = []

    for item in items[:MAX_LIST_ENTRIES]:
        try:
            is_directory = item.is_dir()
            size_bytes = None if is_directory else item.stat().st_size
        except OSError:
            continue

        entries.append(
            {
                "name": item.name,
                "type": "dir" if is_directory else "file",
                "size_bytes": size_bytes,
            }
        )

    return {
        "success": True,
        "path": _relative(path),
        "entries": entries,
        "truncated": truncated,
    }


# ==========================================
# 执行工具
# ==========================================

def run_shell(tool_call, cancel_token=None) -> dict:
    args = _arguments(tool_call)
    command = args.get("command")

    if not isinstance(command, str) or not command.strip():
        raise ValueError("command 不能为空")

    completed = subprocess.run(
        command,
        shell=True,
        cwd=str(workspace_root()),
        capture_output=True,
        check=False,
    )

    return {
        "success": True,
        "exit_code": int(completed.returncode),
        "stdout": _text(completed.stdout),
        "stderr": _text(completed.stderr),
    }


def run_python(tool_call, cancel_token=None) -> dict:
    args = _arguments(tool_call)
    code = args.get("code")

    if not isinstance(code, str) or not code.strip():
        raise ValueError("code 不能为空")

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(workspace_root()),
        capture_output=True,
        check=False,
    )

    return {
        "success": True,
        "exit_code": int(completed.returncode),
        "stdout": _text(completed.stdout),
        "stderr": _text(completed.stderr),
    }


def http_get(tool_call, cancel_token=None) -> dict:
    args = _arguments(tool_call)
    url = args.get("url")

    if not isinstance(url, str) or not url.strip():
        raise ValueError("url 不能为空")

    parsed = urlsplit(url)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("只支持 http / https URL")

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "personal-agent-tier1/1.0"},
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request, timeout=HTTP_TIMEOUT_SECONDS
        ) as response:
            status = int(response.status)
            data = response.read(MAX_HTTP_BYTES + 1)

    except (urllib.error.URLError, OSError, ValueError) as exc:
        # 统一成 ValueError，宿主才能按工具错误而不是 runner 崩溃处理。
        raise ValueError(f"HTTP 请求失败：{exc}") from None

    truncated = len(data) > MAX_HTTP_BYTES

    return {
        "success": True,
        "url": url,
        "status": status,
        "content": _text(data[:MAX_HTTP_BYTES]),
        "truncated": truncated,
    }


# ==========================================
# schema 与注册表
# ==========================================

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取沙箱工作区里的一个文件（只接受相对路径）。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作区内的相对路径"},
            },
            "required": ["path"],
        },
    },
}

LIST_DIR_TOOL = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "列出沙箱工作区目录内容（只接受相对路径）。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作区内的相对路径，默认根目录"},
            },
            "required": [],
        },
    },
}

WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "在沙箱工作区写入一个文本文件（只接受相对路径）。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
}

RUN_SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": "在沙箱工作区执行一条 shell 命令。",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}

RUN_PYTHON_TOOL = {
    "type": "function",
    "function": {
        "name": "run_python",
        "description": "在沙箱工作区执行一段 Python 代码。",
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
}

HTTP_GET_TOOL = {
    "type": "function",
    "function": {
        "name": "http_get",
        "description": "GET 一个 http(s) URL，输出有上限。",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
}


TOOL_DEFINITIONS = (
    (READ_FILE_TOOL, "allow", read_file),
    (LIST_DIR_TOOL, "allow", list_dir),
    # 以下四个在 D13 接入审批门之前默认 ask：模型即使猜到名字也不会被执行。
    (WRITE_FILE_TOOL, "ask", write_file),
    (RUN_SHELL_TOOL, "ask", run_shell),
    (RUN_PYTHON_TOOL, "ask", run_python),
    (HTTP_GET_TOOL, "ask", http_get),
)

# 直接暴露给模型的安全子集。
MODEL_VISIBLE_TOOLS = (READ_FILE_TOOL, LIST_DIR_TOOL)
