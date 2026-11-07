"""ScoreCard：通用 60 分 + 专项 40 分，全部从**可测量的事实**推导。

为什么不调模型打分：验收要能重复、能复核、能算钱。模型打分每次都可能飘，
还额外烧 token。这里每一条都对应一个可断言的事实（字数、来源数、引用命中、
链路通过、token 用量、通知记录），任何人重跑都能得到同样的分。

诚实标注：这是**机器代理 rubric**，不是人工评审，也不冒充人工评审。
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field

from src.research.report import check_report


GENERIC_MAX = 60
REPORT_MAX = 40
SNAKE_MAX = 40
PASS_SCORE = 85
AVERAGE_SCORE = 90

_CITATION = re.compile(r"\[E(\d+)\]")
_HEADING = re.compile(r"^##\s+", re.MULTILINE)


class ScoreError(ValueError):
    """评分输入不合法。"""


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    score: float
    maximum: float
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "score": round(self.score, 1),
            "max": self.maximum,
            "note": self.note,
        }


@dataclass(frozen=True)
class ScoreCard:
    kind: str
    items: tuple[Item, ...]
    hard_gate_failures: tuple[str, ...] = ()

    @property
    def total(self) -> float:
        return sum(item.score for item in self.items)

    @property
    def maximum(self) -> float:
        return sum(item.maximum for item in self.items)

    @property
    def passed(self) -> bool:
        return not self.hard_gate_failures and self.total >= PASS_SCORE

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "total": round(self.total, 1),
            "max": self.maximum,
            "passed": self.passed,
            "hard_gate_failures": list(self.hard_gate_failures),
            "items": [item.to_dict() for item in self.items],
        }


def _band(value: float, thresholds: tuple[tuple[float, float], ...], default=0.0) -> float:
    """按"达到阈值就给分"取最高档。thresholds 从高到低。"""

    for threshold, score in thresholds:
        if value >= threshold:
            return score

    return default


# --------------------------------------------------------------------------
# 通用 60 分
# --------------------------------------------------------------------------

def generic_items(
    *,
    artifact_exists: bool,
    artifact_opens: bool,
    citations_ok: bool,
    reference_count: int,
    crash_free: bool,
    safety_ok: bool,
    within_budget: bool,
    notified: bool,
) -> tuple[Item, ...]:
    """通用 60 分：需求 15 / 可验证性 15 / 鲁棒性 10 / 安全 10 / 效率 5 / 交付 5。"""

    requirement = 15.0 if (artifact_exists and artifact_opens) else 0.0

    verifiable = 15.0 if citations_ok else (
        8.0 if reference_count > 0 else 0.0
    )

    robust = 10.0 if crash_free else 0.0
    safety = 10.0 if safety_ok else 0.0
    efficient = 5.0 if within_budget else 2.0
    delivery = 5.0 if notified else 0.0

    return (
        Item("requirement", "需求达成", requirement, 15.0),
        Item(
            "verifiable",
            "可验证性",
            verifiable,
            15.0,
            f"引用命中={citations_ok}，来源 {reference_count} 条",
        ),
        Item("robust", "鲁棒性", robust, 10.0),
        Item("safety", "安全", safety, 10.0),
        Item("efficiency", "效率", efficient, 5.0),
        Item("delivery", "交付通知", delivery, 5.0),
    )


# --------------------------------------------------------------------------
# 专项 40：研究报告
# --------------------------------------------------------------------------

def report_items(
    markdown: str,
    *,
    source_count: int,
    evidence_store=None,
) -> tuple[Item, ...]:
    """报告专项 40：证据密度 10 / 结构 8 / 来源 10 / 诚实 6 / 多样性 6。"""

    if not isinstance(markdown, str) or not markdown.strip():
        raise ScoreError("报告为空")

    body, _, appendix = markdown.partition("## 参考来源")
    chars = len("".join(body.split()))
    citations = _CITATION.findall(body)
    density = (len(citations) / chars * 1000) if chars else 0.0

    density_score = _band(
        density, ((6.0, 10.0), (4.0, 8.0), (2.0, 5.0)), default=2.0
    )

    headings = _HEADING.findall(body)
    structure = 8.0
    structure -= 3.0 if len(headings) < 3 else 0.0
    structure -= 2.0 if "## 摘要" not in body else 0.0
    structure -= 3.0 if "## 局限" not in body and "局限与未解问题" not in body else 0.0
    structure = max(0.0, structure)

    source_score = _band(
        source_count, ((10, 10.0), (7, 7.0), (4, 4.0)), default=0.0
    )

    honest_score = 6.0 if ("局限" in body or "limitations" in body.lower()) else 0.0

    domains = set(
        re.findall(r"https?://([^/)\s]+)", appendix or markdown)
    )
    diversity = _band(len(domains), ((5, 6.0), (3, 4.0)), default=2.0)

    return (
        Item("evidence_density", "证据密度", density_score, 10.0, f"{density:.1f} 处/千字"),
        Item("structure", "结构完整", structure, 8.0, f"{len(headings)} 个小节"),
        Item("sources", "来源数量", source_score, 10.0, f"{source_count} 条"),
        Item("honesty", "诚实标注", honest_score, 6.0),
        Item("diversity", "来源多样性", diversity, 6.0, f"{len(domains)} 个域名"),
    )


# --------------------------------------------------------------------------
# 专项 40：贪吃蛇
# --------------------------------------------------------------------------

def snake_items(
    *,
    loads: bool,
    controls_ok: bool,
    score_works: bool,
    game_over_works: bool,
    ticks_500_ok: bool,
    mobile_ok: bool,
    draws: bool,
) -> tuple[Item, ...]:
    """贪吃蛇专项 40：可玩 10 / 规则 10 / 移动端 8 / 稳定 6 / 视觉 6。"""

    playable = _band(
        sum([loads, controls_ok, score_works, game_over_works]),
        ((4, 10.0), (3, 7.0), (2, 4.0)),
        default=0.0,
    )
    rules = 10.0 if (score_works and game_over_works) else 5.0
    mobile = 8.0 if mobile_ok else 0.0
    stable = 6.0 if ticks_500_ok else 0.0
    visual = 6.0 if draws else 0.0

    return (
        Item("playable", "可玩性", playable, 10.0),
        Item("rules", "规则完整", rules, 10.0),
        Item("mobile", "移动端", mobile, 8.0),
        Item("stable", "稳定性", stable, 6.0, "500 tick"),
        Item("visual", "视觉呈现", visual, 6.0),
    )


# --------------------------------------------------------------------------
# 组装 + 硬门槛
# --------------------------------------------------------------------------

def score_report(
    markdown: str,
    *,
    source_count: int,
    artifact_opens: bool = True,
    evidence_store=None,
    crash_free: bool = True,
    safety_ok: bool = True,
    within_budget: bool = True,
    notified: bool = True,
    tokens: int = 0,
) -> ScoreCard:
    """报告验收：通用 60 + 专项 40，外加硬门槛。"""

    gates = []

    if not (markdown or "").strip():
        gates.append("产物不存在")

    elif not artifact_opens:
        gates.append("产物打不开")

    if evidence_store is not None:
        missing = check_report(markdown, evidence_store)

        if missing:
            gates.append(f"引用无法命中：{[f'E{item}' for item in missing]}")

    if not crash_free:
        gates.append("执行崩溃")

    if gate_failures := list(gates):
        return ScoreCard(kind="report", items=(), hard_gate_failures=tuple(gate_failures))

    items = generic_items(
        artifact_exists=True,
        artifact_opens=artifact_opens,
        citations_ok=evidence_store is not None,
        reference_count=source_count,
        crash_free=crash_free,
        safety_ok=safety_ok,
        within_budget=within_budget,
        notified=notified,
    ) + report_items(markdown, source_count=source_count)

    return ScoreCard(kind="report", items=items)


def score_snake(
    *,
    loads: bool,
    controls_ok: bool,
    score_works: bool,
    game_over_works: bool,
    ticks_500_ok: bool,
    mobile_ok: bool,
    draws: bool,
    crash_free: bool = True,
    safety_ok: bool = True,
    within_budget: bool = True,
    notified: bool = True,
) -> ScoreCard:
    gates = []

    if not loads:
        gates.append("产物打不开")

    if not crash_free:
        gates.append("执行崩溃")

    if gates:
        return ScoreCard(kind="snake", items=(), hard_gate_failures=tuple(gates))

    items = generic_items(
        artifact_exists=True,
        artifact_opens=loads,
        citations_ok=True,
        reference_count=0,
        crash_free=crash_free,
        safety_ok=safety_ok,
        within_budget=within_budget,
        notified=notified,
    ) + snake_items(
        loads=loads,
        controls_ok=controls_ok,
        score_works=score_works,
        game_over_works=game_over_works,
        ticks_500_ok=ticks_500_ok,
        mobile_ok=mobile_ok,
        draws=draws,
    )

    return ScoreCard(kind="snake", items=items)


def summarize(cards: list[ScoreCard], *, average_target: float = AVERAGE_SCORE) -> dict:
    """汇总多次运行：是否全部 ≥85 且平均 ≥90。"""

    if not cards:
        raise ScoreError("没有可汇总的评分卡")

    totals = [card.total for card in cards]
    average = sum(totals) / len(totals)
    every_pass = all(card.passed for card in cards)
    spread = max(totals) - min(totals)

    return {
        "runs": len(cards),
        "totals": [round(total, 1) for total in totals],
        "average": round(average, 1),
        "min": round(min(totals), 1),
        "max": round(max(totals), 1),
        "spread": round(spread, 1),
        "every_run_passed": every_pass,
        "average_ok": average >= average_target,
        "overall": every_pass and average >= average_target,
        "spread_warning": spread > 5,
    }
