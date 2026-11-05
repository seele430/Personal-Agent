"""意图路由：只给约束，不做执行。

职责边界（见 docs/design-intent-router.md）：
- 判断想做什么、是否澄清、能力/模型策略、风险与审批建议、回退白名单；
- 不选节点、不查配额、不创建沙箱。
"""

from __future__ import annotations

import re
import unicodedata

from dataclasses import asdict, dataclass
from typing import Literal

Intent = Literal[
    "general.chat",
    "data.analysis",
    "code.execute",
    "file.operate",
    "platform.inspect",
    "reminder.manage",
]

Risk = Literal["low", "medium", "high"]

CLARIFY_THRESHOLD = 0.55
CLARIFY_GAP = 0.30


@dataclass(frozen=True)
class RouteDecision:
    decision: Literal["route", "clarify"]
    intent: str | None
    confidence: float
    risk: Risk
    requires_approval: bool
    capability: str
    execution_type: str | None
    model_policy: Literal["cheap", "strong", "local"]
    constraints: dict
    fallback: tuple[str, ...]
    candidates: tuple[tuple[str, float], ...]
    reason: str
    source: Literal["rule", "classifier", "llm", "clarify"]

    def to_event(self) -> dict:
        payload = asdict(self)
        payload["event"] = "route_decided"
        payload["fallback"] = list(self.fallback)
        payload["candidates"] = [list(item) for item in self.candidates]
        return payload


# (intent, 正则, 标签, 权重)
_RULES: tuple[tuple[str, re.Pattern[str], str, float], ...] = (
    (
        "data.analysis",
        re.compile(
            r"分析|统计|缺失值|数据(清洗|处理)|csv|excel|表格|图表|可视化|"
            r"画(一|个|张)?图|pandas|numpy|平均值|分布|相关性",
            re.IGNORECASE,
        ),
        "analysis_kw",
        1.0,
    ),
    (
        "code.execute",
        re.compile(
            r"运行|执行|跑(一下|一遍)?|脚本|命令|报错|复现|python|node|"
            r"pip install|pytest|编译",
            re.IGNORECASE,
        ),
        "exec_kw",
        1.0,
    ),
    (
        "file.operate",
        re.compile(
            r"文件|目录|文件夹|写入|读取|保存|创建|重命名|移动|复制|"
            r"打开.+\.(txt|md|csv|json|py)",
            re.IGNORECASE,
        ),
        "file_kw",
        0.9,
    ),
    (
        "platform.inspect",
        re.compile(
            r"指标|节点|队列|沙箱|平台(状态|健康|负载)|metrics|queue|"
            r"执行记录|审计",
            re.IGNORECASE,
        ),
        "platform_kw",
        0.9,
    ),
    (
        "reminder.manage",
        re.compile(r"提醒|闹钟|定时|每天|明天.+(点|时)|remind", re.IGNORECASE),
        "reminder_kw",
        1.0,
    ),
)

_HIGH_RISK = re.compile(
    r"删除|删掉|清空|格式化|rm\s+-rf|del\s+/[fsq]|drop\s+table|"
    r"覆盖|提权|sudo|注册表|密码|密钥|token|\.env|"
    r"drop\s+database|truncate",
    re.IGNORECASE,
)

_MEDIUM_RISK = re.compile(
    r"写入|保存|创建|重命名|移动|复制|修改|安装|卸载",
    re.IGNORECASE,
)

_CAPABILITY: dict[str, tuple[str, str | None]] = {
    "general.chat": ("chat", None),
    "data.analysis": ("sandbox_exec", "local_sandbox"),
    "code.execute": ("sandbox_exec", "local_sandbox"),
    "file.operate": ("sandbox_exec", "local_sandbox"),
    "platform.inspect": ("platform_read", None),
    "reminder.manage": ("reminder", None),
}

_FALLBACK: dict[str, tuple[str, ...]] = {
    "data.analysis": ("code.execute",),
    "code.execute": ("general.chat",),
    "file.operate": ("code.execute",),
    "platform.inspect": (),
    "reminder.manage": (),
    "general.chat": (),
}

_DEFAULT_TOOLS: dict[str, tuple[str, ...]] = {
    "data.analysis": ("python", "write_file", "list_dir"),
    "code.execute": ("python", "shell", "list_dir"),
    "file.operate": ("write_file", "read_file", "list_dir"),
}


def normalize(message: str) -> str:
    text = unicodedata.normalize("NFKC", message or "")

    return re.sub(r"\s+", " ", text).strip()


def _confidence(top: float, second: float) -> float:
    if top <= 0:
        return 0.60

    score = 0.62 + 0.12 * min(top, 3.0) + 0.10 * max(0.0, top - second)

    return round(min(0.95, score), 2)


class Router:
    """确定性规则路由（初版）；分类器层后续接入，接口不变。"""

    def decide(self, message: str) -> RouteDecision:
        text = normalize(message)

        scores: dict[str, float] = {}
        hits: list[str] = []

        for intent, pattern, label, weight in _RULES:
            if pattern.search(text):
                scores[intent] = scores.get(intent, 0.0) + weight
                hits.append(label)

        if not scores:
            scores["general.chat"] = 0.3
            hits.append("default_chat")

        ranked = tuple(
            sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:3]
        )

        top_intent, top_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0

        confidence = _confidence(top_score, second_score)

        ambiguous = (
            top_score > 0
            and second_score > 0
            and (top_score - second_score) < CLARIFY_GAP
            and top_intent != "general.chat"
        )

        if confidence < CLARIFY_THRESHOLD or ambiguous:
            return RouteDecision(
                decision="clarify",
                intent=None,
                confidence=confidence,
                risk="low",
                requires_approval=False,
                capability="chat",
                execution_type=None,
                model_policy="cheap",
                constraints={},
                fallback=(),
                candidates=ranked,
                reason=(
                    "ambiguous:" + ",".join(hits)
                    if ambiguous
                    else "low_confidence:" + ",".join(hits)
                ),
                source="clarify",
            )

        risk: Risk = "low"

        if _HIGH_RISK.search(text):
            risk = "high"
        elif _MEDIUM_RISK.search(text):
            risk = "medium"

        capability, execution_type = _CAPABILITY[top_intent]

        constraints: dict = {}

        if top_intent in _DEFAULT_TOOLS:
            constraints = {
                "timeout_ms": 120_000,
                "allowed_tools": list(_DEFAULT_TOOLS[top_intent]),
                "artifact_required": top_intent == "data.analysis",
            }

        return RouteDecision(
            decision="route",
            intent=top_intent,
            confidence=confidence,
            risk=risk,
            requires_approval=risk == "high",
            capability=capability,
            execution_type=execution_type,
            model_policy="strong" if top_intent == "data.analysis" else "cheap",
            constraints=constraints,
            fallback=_FALLBACK[top_intent],
            candidates=ranked,
            reason="rule:" + ",".join(hits),
            source="rule",
        )
