"""权限策略：allow / confirm / deny，路径校验与确认定位符。

安全约定：

- 路径：workspace 先解析真实路径；目标必须已存在；逐段拒绝符号链接与 reparse point；
  规范化后必须 `is_relative_to(workspace)`；只允许普通文件且 ≤200 KB；
- 确认：`confirmation_id` 是**非密钥定位符**（8 位 Crockford Base32），明文存储；
- 残余风险：普通 Python 文件 API 存在检查与打开之间的 TOCTOU，本模块不宣称消除。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat

from pathlib import Path


MAX_READ_BYTES = 200 * 1024
CONFIRM_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CONFIRM_TTL_SECONDS = 60


DEVICE_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)


class PolicyError(ValueError):
    """策略拒绝（中文短句，不暴露主机路径结构）。"""


def generate_confirmation_id() -> str:
    return "".join(secrets.choice(CONFIRM_ALPHABET) for _ in range(8))


def params_hash(params: dict) -> str:
    payload = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(), "st_file_attributes", 0)
    except OSError:
        return True

    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def validate_read_path(raw: str, workspace: Path) -> Path:
    text = (raw or "").strip()

    if not text:
        raise PolicyError("路径不能为空")

    if ".." in text.split("/") or ".." in text.split("\\"):
        raise PolicyError("路径不允许包含 ..")

    if re_match_drive(text):
        raise PolicyError("只能读取工作区内的相对路径")

    for part in Path(text).parts:
        if DEVICE_NAME.match(part):
            raise PolicyError("不允许访问 Windows 保留设备名")

    if text.startswith("\\\\") or text.startswith("//"):
        raise PolicyError("不支持网络路径")

    if ":" in text:
        raise PolicyError("不支持备用数据流或盘符")

    workspace_real = workspace.resolve(strict=True)
    raw_target = workspace_real / text

    # 先检查**未解析**的路径链：resolve 会把符号链接解析掉，之后再看就看不到了
    current = raw_target

    while current != workspace_real and current != current.parent:
        if current.is_symlink() or _is_reparse_point(current):
            raise PolicyError("不允许读取符号链接或重解析点")

        current = current.parent

    try:
        target = raw_target.resolve(strict=True)
    except FileNotFoundError:
        raise PolicyError("文件不存在") from None
    except OSError:
        raise PolicyError("路径无法解析") from None

    if not target.is_relative_to(workspace_real):
        raise PolicyError("只能读取工作区内的文件")

    current = target

    while True:
        if current.is_symlink() or _is_reparse_point(current):
            raise PolicyError("不允许读取符号链接或重解析点")

        if current == workspace_real:
            break

        current = current.parent

    if not target.is_file():
        raise PolicyError("只能读取普通文件")

    size = target.stat().st_size

    if size > MAX_READ_BYTES:
        raise PolicyError(f"文件过大（上限 {MAX_READ_BYTES // 1024} KB）")

    return target


def re_match_drive(text: str) -> bool:
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha()


def read_text_checked(path: Path) -> str:
    """读取后再核对大小，缩小检查与读取之间的窗口。"""

    before = path.stat().st_size

    if before > MAX_READ_BYTES:
        raise PolicyError(f"文件过大（上限 {MAX_READ_BYTES // 1024} KB）")

    data = path.read_bytes()
    after = path.stat().st_size

    if after != before or len(data) != before:
        raise PolicyError("文件在读取过程中发生变化")

    return data.decode("utf-8", errors="replace")


def truncate_for_qq(text: str, limit: int = 3000) -> str:
    if len(text) <= limit:
        return text

    head = limit * 2 // 3
    tail = limit - head

    return text[:head] + "\n…（中间已省略）…\n" + text[-tail:]


def chunk_for_qq(
    text: str, limit: int = 800, max_chunks: int = 3, max_chars: int = 4500
) -> list[str]:
    """把长回答切成若干条私聊；按空行/句末切，条数超上限时按字数均衡重切。"""

    text = (text or "").strip()

    if not text:
        return []

    if len(text) <= limit:
        return [text]

    max_chunks = max(1, max_chunks)
    pieces: list[str] = []
    current = ""

    for block in text.split("\n\n"):
        block = block.strip("\n")

        if not block:
            continue

        for part in _split_long_block(block, limit):
            if not current:
                current = part
            elif len(current) + 2 + len(part) <= limit:
                current = current + "\n\n" + part
            else:
                pieces.append(current)
                current = part

    if current:
        pieces.append(current)

    if len(pieces) <= max_chunks:
        return pieces

    # 条数超上限：按字数均衡重切（QQ 单条约 4500 字上限），避免出现一条巨长 + 一条几个字
    target = min(max(-(-len(text) // max_chunks), limit), max_chars)
    balanced = _split_long_block(text, target)

    if len(balanced) <= max_chunks:
        return balanced

    return balanced[: max_chunks - 1] + ["\n\n".join(balanced[max_chunks - 1:])]


def _split_long_block(block: str, limit: int) -> list[str]:
    parts: list[str] = []
    rest = block

    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind(mark) for mark in "。！？!?；;\n")

        if cut < limit // 2:
            cut = limit
        else:
            cut += 1

        parts.append(rest[:cut].strip("\n"))
        rest = rest[cut:].lstrip("\n")

    if rest:
        parts.append(rest)

    return [part for part in parts if part]


def decide(*, intent: str, params: dict, route_action: str, workspace: Path) -> dict:
    """把路由结果转成策略决定。"""

    if route_action == "deny":
        return {"decision": "deny", "reason": "dangerous"}

    if route_action == "clarify":
        return {"decision": "clarify", "reason": "need_more_info"}

    if route_action == "unavailable":
        return {"decision": "unavailable", "reason": "not_open"}

    if intent == "read_file":
        try:
            target = validate_read_path(params.get("path", ""), workspace)
        except PolicyError as error:
            return {"decision": "deny", "reason": str(error)}

        return {
            "decision": "confirm",
            "reason": "read_workspace_file",
            "confirm_id": generate_confirmation_id(),
            "params_hash": params_hash(params),
            "path": str(target),
            "ttl": CONFIRM_TTL_SECONDS,
        }

    return {"decision": "allow", "reason": "ok"}


def ensure_workspace(path: str | Path) -> Path:
    workspace = Path(path).resolve()

    if not workspace.is_dir():
        raise PolicyError("工作区不存在")

    return workspace


def file_size(path: Path) -> int:
    return os.stat(path).st_size
