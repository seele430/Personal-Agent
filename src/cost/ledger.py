"""成本账本：把"花了多少 token / 每个被接受的产物多少钱"算清楚。

三条口径（对应路线图 §10.2）：

1. **单价是参数，不是常量**。至今没有权威单价，所以账本只记 token 与
   "按给定价目表折算"的金额；价目表为空时只报用量，**不编人民币数字**。
2. **失败运行也算钱**。`cost_per_accepted_artifact` 的分子包含失败与重跑，
   否则"多试几次总能过"会被算成零成本。
3. **按产物聚合**，不按"调用次数"聚合 —— 验收关心的是"一个能交付的产物
   花了多少"，不是"发了多少次请求"。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class LedgerError(ValueError):
    """账本输入不合法。"""


@dataclass(frozen=True)
class Entry:
    run_id: str
    kind: str                 # report / snake / repair / chat ...
    tokens_in: int = 0
    tokens_out: int = 0
    accepted: bool = False    # 这次运行是否产出了被接受的产物
    artifact_id: str = ""
    note: str = ""

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "total_tokens": self.total_tokens,
            "accepted": self.accepted,
            "artifact_id": self.artifact_id,
            "note": self.note,
        }


@dataclass(frozen=True)
class Price:
    """每百万 token 的价格。货币单位由调用方自己解释。"""

    input_per_million: float
    output_per_million: float

    def __post_init__(self):
        for name, value in (
            ("input_per_million", self.input_per_million),
            ("output_per_million", self.output_per_million),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise LedgerError(f"{name} 必须是数字")

            if value < 0:
                raise LedgerError(f"{name} 不能为负")


class CostLedger:
    def __init__(self, *, price: Price | None = None):
        self.price = price
        self._entries: list[Entry] = []

    def record(
        self,
        run_id: str,
        *,
        kind: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        accepted: bool = False,
        artifact_id: str = "",
        note: str = "",
    ) -> Entry:
        if not isinstance(run_id, str) or not run_id.strip():
            raise LedgerError("run_id 不能为空")

        if not isinstance(kind, str) or not kind.strip():
            raise LedgerError("kind 不能为空")

        for name, value in (("tokens_in", tokens_in), ("tokens_out", tokens_out)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise LedgerError(f"{name} 必须是非负整数")

        entry = Entry(
            run_id=run_id.strip(),
            kind=kind.strip(),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            accepted=bool(accepted),
            artifact_id=artifact_id,
            note=note,
        )

        self._entries.append(entry)

        return entry

    # ---- 汇总 -------------------------------------------------------

    def summary(self) -> dict:
        total_in = sum(item.tokens_in for item in self._entries)
        total_out = sum(item.tokens_out for item in self._entries)
        accepted = [item for item in self._entries if item.accepted]
        by_kind: dict[str, dict] = {}

        for item in self._entries:
            bucket = by_kind.setdefault(
                item.kind,
                {"runs": 0, "tokens": 0, "accepted": 0},
            )
            bucket["runs"] += 1
            bucket["tokens"] += item.total_tokens
            bucket["accepted"] += 1 if item.accepted else 0

        cost = self._cost(total_in, total_out)
        accepted_count = len(accepted)

        return {
            "runs": len(self._entries),
            "accepted_runs": accepted_count,
            "tokens_in": total_in,
            "tokens_out": total_out,
            "total_tokens": total_in + total_out,
            "cost": cost,
            "cost_per_accepted_artifact": (
                None if accepted_count == 0 or cost is None
                else round(cost / accepted_count, 6)
            ),
            "tokens_per_accepted_artifact": (
                None if accepted_count == 0
                else round((total_in + total_out) / accepted_count, 1)
            ),
            "by_kind": by_kind,
            "price_configured": self.price is not None,
        }

    def _cost(self, tokens_in: int, tokens_out: int) -> float | None:
        if self.price is None:
            return None

        return round(
            tokens_in / 1_000_000 * self.price.input_per_million
            + tokens_out / 1_000_000 * self.price.output_per_million,
            6,
        )

    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries)


def summary_from_records(records: list[dict], *, price: Price | None = None) -> dict:
    """从 JSON 记录批量建账（验收证据里的 runs 列表）。"""

    if not isinstance(records, list):
        raise LedgerError("records 必须是列表")

    ledger = CostLedger(price=price)

    for record in records:
        if not isinstance(record, dict):
            raise LedgerError("每条记录必须是对象")

        ledger.record(
            str(record.get("run_id") or record.get("id") or "unknown"),
            kind=str(record.get("kind") or "report"),
            tokens_in=int(record.get("tokens_in") or 0),
            tokens_out=int(record.get("tokens_out") or 0),
            accepted=bool(record.get("accepted")),
            artifact_id=str(record.get("artifact_id") or ""),
            note=str(record.get("note") or ""),
        )

    return ledger.summary()
