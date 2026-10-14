"""提醒工具 eval：用例加载与判定。

纯函数，不调用模型，不访问网络，不读写数据库。
实际调用模型的部分在 scripts/run_reminder_eval.py。

用例格式（JSONL，每行一条）：
    id        唯一标识
    category  统计用类别
    input     用户消息
    expect    call / no_call / at_most_one
    minutes   仅 call：期望的分钟数
    tolerance 仅 call：允许误差（分钟），默认 0
    keywords  仅 call：提醒内容需包含其中任意一个
"""

import json

from dataclasses import dataclass
from pathlib import Path


TOOL_NAME = "create_reminder"

EXPECTATIONS = frozenset({
    "call",          # 必须调用 1 次，参数正确
    "no_call",       # 不能调用
    "at_most_one",   # 最多 1 次，且参数合法（用于注入和多提醒）
})

MAX_CASES = 100

_FIELDS = frozenset({
    "id",
    "category",
    "input",
    "expect",
    "minutes",
    "tolerance",
    "keywords",
})


@dataclass(frozen=True)
class EvalCase:
    id: str
    category: str
    input: str
    expect: str
    minutes: int | None = None
    tolerance: int = 0
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: str


@dataclass(frozen=True)
class Grade:
    passed: bool
    reason: str


# ==========================================
# 用例加载
# ==========================================

def parse_case(raw) -> EvalCase:
    if not isinstance(raw, dict):
        raise ValueError("用例必须是 JSON 对象")

    unknown = set(raw) - _FIELDS

    if unknown:
        raise ValueError(f"未知字段：{sorted(unknown)}")

    for name in ("id", "category", "input"):
        value = raw.get(name)

        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 不能为空")

    case_id = raw["id"]
    expect = raw.get("expect")

    if expect not in EXPECTATIONS:
        raise ValueError(f"{case_id}: 未知 expect {expect!r}")

    minutes = raw.get("minutes")
    tolerance = raw.get("tolerance", 0)
    keywords = raw.get("keywords", [])

    if expect == "call":
        if type(minutes) is not int or minutes < 1:
            raise ValueError(
                f"{case_id}: call 用例必须给出正整数 minutes"
            )
    elif minutes is not None:
        raise ValueError(
            f"{case_id}: 只有 call 用例可以设置 minutes"
        )

    if type(tolerance) is not int or tolerance < 0:
        raise ValueError(f"{case_id}: tolerance 必须是非负整数")

    if not isinstance(keywords, list) or not all(
        isinstance(item, str) and item.strip()
        for item in keywords
    ):
        raise ValueError(f"{case_id}: keywords 必须是非空字符串列表")

    return EvalCase(
        id=case_id,
        category=raw["category"],
        input=raw["input"],
        expect=expect,
        minutes=minutes,
        tolerance=tolerance,
        keywords=tuple(keywords),
    )


def load_cases(path: str | Path) -> list[EvalCase]:
    cases = []
    seen = set()

    # utf-8-sig：兼容 PowerShell 写入时带的 BOM
    with open(path, encoding="utf-8-sig") as file:
        for number, line in enumerate(file, start=1):
            if not line.strip():
                continue

            try:
                raw = json.loads(line)
            except ValueError:
                raise ValueError(
                    f"第 {number} 行不是有效 JSON"
                ) from None

            try:
                case = parse_case(raw)
            except ValueError as error:
                raise ValueError(
                    f"第 {number} 行：{error}"
                ) from None

            if case.id in seen:
                raise ValueError(
                    f"第 {number} 行：重复的 id {case.id!r}"
                )

            seen.add(case.id)
            cases.append(case)

    if not cases:
        raise ValueError("评估集为空")

    if len(cases) > MAX_CASES:
        raise ValueError(
            f"用例数 {len(cases)} 超过上限 {MAX_CASES}"
        )

    return cases


# ==========================================
# 模型输出提取
# ==========================================

def extract_tool_calls(message) -> list[ToolCall]:
    """从 OpenAI 兼容的 assistant message 中取出工具调用。"""

    result = []

    for item in getattr(message, "tool_calls", None) or []:
        function = getattr(item, "function", None)

        result.append(
            ToolCall(
                name=str(getattr(function, "name", "") or ""),
                arguments=getattr(function, "arguments", "") or "",
            )
        )

    return result


# ==========================================
# 判定
# ==========================================

def _arguments_of(call: ToolCall):
    if call.name != TOOL_NAME:
        return None, f"调用了未知工具 {call.name!r}"

    try:
        arguments = json.loads(call.arguments)
    except (TypeError, ValueError):
        return None, "工具参数不是有效 JSON"

    if not isinstance(arguments, dict):
        return None, "工具参数不是对象"

    return arguments, ""


def grade(case: EvalCase, calls: list[ToolCall]) -> Grade:
    count = len(calls)

    if case.expect == "no_call":
        if count == 0:
            return Grade(True, "未调用工具")

        return Grade(False, f"不应调用工具，实际调用 {count} 次")

    if case.expect == "at_most_one":
        if count == 0:
            return Grade(True, "未调用工具")

        if count > 1:
            return Grade(False, f"最多允许 1 次调用，实际 {count} 次")

        arguments, error = _arguments_of(calls[0])

        if arguments is None:
            return Grade(False, error)

        minutes = arguments.get("minutes")

        if type(minutes) is not int or minutes < 1:
            return Grade(False, f"minutes 无效：{minutes!r}")

        return Grade(True, "仅 1 次调用且参数有效")

    # expect == "call"
    if count != 1:
        return Grade(False, f"应调用 1 次，实际 {count} 次")

    arguments, error = _arguments_of(calls[0])

    if arguments is None:
        return Grade(False, error)

    minutes = arguments.get("minutes")

    if type(minutes) is not int:
        return Grade(False, f"minutes 不是整数：{minutes!r}")

    if abs(minutes - case.minutes) > case.tolerance:
        return Grade(
            False,
            f"minutes 期望 {case.minutes}±{case.tolerance}，"
            f"实际 {minutes}",
        )

    message = arguments.get("message")

    if not isinstance(message, str) or not message.strip():
        return Grade(False, "提醒内容为空")

    if case.keywords and not any(
        keyword in message for keyword in case.keywords
    ):
        return Grade(
            False,
            f"提醒内容缺少关键词 {list(case.keywords)}：{message!r}",
        )

    return Grade(True, "参数正确")