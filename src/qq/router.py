"""纯规则意图路由（无模型、无网络调用）。

顺序（与审核版 v1.2 §6 一致）：

1. 危险操作 → deny
2. 精确命令 /status /cancel /confirm
3. 读文件意图 + 明确路径 → read_file
4. 读文件意图但缺路径 → clarify
5. 已识别但未开放（画图/写文件） → unavailable
6. 普通文本 → ask
7. 其余 → clarify
"""

from __future__ import annotations

import re


# 危险操作：只做拒绝识别，绝不执行
DANGER_PATTERNS = (
    r"\brm\s+-rf\b",
    r"\bformat\b",
    r"\bdel\s+/[fqs]\b",
    r"\b(shutdown|reboot)\b",
    r"执行(一下|命令)?",
    r"运行(一下)?命令",
    r"(删除|删掉|清空|格式化)(文件|目录|磁盘|整个)",
    r"改(配置|密码|密钥)",
)

READ_PATTERNS = (
    r"读(一下|取|取一下)?",
    r"看看",
    r"查看",
    r"打开",
    r"内容是",
)

UNAVAILABLE_PATTERNS = (
    r"画(一|个|只|张)",
    r"(生成|做|来)(一张|个)图",
    r"画图",
    r"(写|存|保存)(成|到).*(文件|md|txt)",
)

CONTINUE_PREFIXES = (
    "继续",
    "接着",
    "再补充",
    "然后呢",
    "继续说",
    "接着写",
)

PATH_PATTERN = re.compile(r"[\w./\\-]*[\w-]+\.(md|txt|py|json|log|csv|ya?ml)")


def _danger(text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in DANGER_PATTERNS)


def _read_intent(text: str) -> bool:
    return any(re.search(pattern, text) for pattern in READ_PATTERNS)


def _unavailable(text: str) -> bool:
    return any(re.search(pattern, text) for pattern in UNAVAILABLE_PATTERNS)


def _path_of(text: str) -> str:
    match = PATH_PATTERN.search(text)

    return match.group(0) if match else ""


def classify(text: str) -> dict:
    """返回 `{intent, params, action, reason, source}`。"""

    message = (text or "").strip()

    if not message:
        return _result("unknown", {}, "clarify", "empty", "rule")

    if _danger(message):
        return _result("exec", {}, "deny", "dangerous", "rule")

    if message in ("/status", "status", "在吗", "在不在"):
        return _result("status", {}, "execute", "status", "command")

    if message.startswith("/cancel"):
        target = message[len("/cancel") :].strip()

        return _result("cancel", {"target": target}, "execute", "cancel", "command")

    if message in ("/reset", "reset"):
        return _result("reset", {}, "execute", "reset", "command")

    if message.startswith("/confirm"):
        confirmation_id = message[len("/confirm") :].strip()

        return _result(
            "confirm", {"confirmation_id": confirmation_id}, "execute", "confirm", "command"
        )

    if message.startswith("/ask"):
        question = message[len("/ask") :].strip()

        if not question:
            return _result("unknown", {}, "clarify", "ask_without_question", "command")

        return _result("ask", {"question": question}, "execute", "ask", "command")

    if message.startswith("/continue"):
        rest = message[len("/continue") :].strip()
        parts = rest.split(None, 1)

        if len(parts) < 2:
            return _result("unknown", {}, "clarify", "continue_usage", "command")

        return _result(
            "continue_run",
            {"run_id": parts[0], "message": parts[1]},
            "execute",
            "continue_run",
            "command",
        )

    if message.startswith("/"):
        return _result("unknown", {}, "clarify", "unknown_command", "command")

    if _read_intent(message):
        path = _path_of(message)

        if not path:
            return _result("unknown", {}, "clarify", "need_path", "rule")

        return _result("read_file", {"path": path}, "confirm", "read_file", "rule")

    if _unavailable(message):
        return _result("unknown", {}, "unavailable", "not_open", "rule")

    if any(message.startswith(prefix) for prefix in CONTINUE_PREFIXES):
        return _result(
            "ask", {"question": message}, "execute", "continue_ask", "rule", use_context=True
        )

    return _result("ask", {"question": message}, "execute", "plain_text_ask", "rule")


def _result(
    intent: str,
    params: dict,
    action: str,
    reason: str,
    source: str,
    *,
    use_context: bool = False,
) -> dict:
    return {
        "intent": intent,
        "params": params,
        "action": action,   # execute / confirm / deny / clarify / unavailable
        "reason": reason,
        "source": source,
        "use_context": use_context,
    }
