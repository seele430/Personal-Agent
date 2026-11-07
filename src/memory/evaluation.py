"""60 条记忆评测：Recall@5、冲突采用当前有效事实、删除不复活、版本回滚。

设计原则（对齐路线图 §9.4 与"不为数字凑证据"）：

- **样本与阈值冻结在夹具里**，先写死再跑；跑完不许回头改样本。
- **免费指标全是确定性判定**：召回靠 key 命中、冲突靠 top1 是否当前事实、
  删除靠"删完再搜必须 0 命中"。不需要模型，也不该花模型的钱。
- **付费指标单独一档**（`run_answer_check`）：让模型在给定记忆上下文下作答，
  要求结构化返回 `citations` / `insufficient`。判"无依据回答"只看两件事：
  声称有依据却给不出引用、或引用了不在上下文里的编号。**不需要第二个裁判模型。**

所以"60 条评测烧钱"其实只烧在最后一档，而且能按条数限流。
"""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from pathlib import Path

from src.memory.models import MemoryError
from src.memory.resolve import resolve_group
from src.memory.store import MemoryStore
from src.memory.versions import MemoryVersions


DEFAULT_DATASET = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "memory" / "samples.json"
)

CATEGORIES = ("preference", "project_fact", "conflict", "deletion", "unanswerable")

RECALL_TARGET = 45          # 50 条可回答样本里至少 45
CONFLICT_TARGET = 10        # 10 条冲突全对
RESURRECTION_TARGET = 0     # 删掉的不许复活
FALSE_HIT_TARGET = 0        # 不可回答样本不许硬答
UNSUPPORTED_TARGET = 1      # 付费档：无依据回答最多 1 条

VERSION_PROBE_KEYS = ("del_code",)


class EvalError(RuntimeError):
    """评测输入或流程错误（不是"样本没通过"）。"""


@dataclass(frozen=True)
class SampleResult:
    id: str
    category: str
    query: str
    passed: bool
    mode: str
    top_keys: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class AnswerResult:
    id: str
    category: str
    insufficient: bool
    citations: tuple[int, ...]
    supported: bool
    detail: str = ""


@dataclass(frozen=True)
class EvalReport:
    seeded: int = 0
    recall_hit: int = 0
    recall_total: int = 0
    conflict_correct: int = 0
    conflict_total: int = 0
    resurrection: int = 0
    deletion_total: int = 0
    false_hits: int = 0
    unanswerable_total: int = 0
    version_rollback_ok: bool = False
    results: tuple[SampleResult, ...] = ()
    problems: tuple[str, ...] = ()
    answers: tuple[AnswerResult, ...] = ()
    unsupported: int = 0
    answer_total: int = 0

    @property
    def recall_rate(self) -> float:
        return self.recall_hit / self.recall_total if self.recall_total else 0.0

    @property
    def passed(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {
            "seeded": self.seeded,
            "recall": {
                "hit": self.recall_hit,
                "total": self.recall_total,
                "target": RECALL_TARGET,
                "rate": round(self.recall_rate, 4),
            },
            "conflict": {
                "correct": self.conflict_correct,
                "total": self.conflict_total,
                "target": CONFLICT_TARGET,
            },
            "deletion": {
                "resurrection": self.resurrection,
                "total": self.deletion_total,
                "target": RESURRECTION_TARGET,
            },
            "unanswerable": {
                "false_hits": self.false_hits,
                "total": self.unanswerable_total,
                "target": FALSE_HIT_TARGET,
            },
            "version_rollback_ok": self.version_rollback_ok,
            "answers": {
                "total": self.answer_total,
                "unsupported": self.unsupported,
                "target": UNSUPPORTED_TARGET,
            },
            "passed": self.passed,
            "problems": list(self.problems),
            "results": [
                {
                    "id": item.id,
                    "category": item.category,
                    "query": item.query,
                    "passed": item.passed,
                    "mode": item.mode,
                    "top_keys": list(item.top_keys),
                    "detail": item.detail,
                }
                for item in self.results
            ],
            "answer_results": [
                {
                    "id": item.id,
                    "category": item.category,
                    "insufficient": item.insufficient,
                    "citations": list(item.citations),
                    "supported": item.supported,
                    "detail": item.detail,
                }
                for item in self.answers
            ],
        }


def load_dataset(path: str | Path | None = None) -> dict:
    target = Path(path or DEFAULT_DATASET)

    if not target.exists():
        raise EvalError(f"评测夹具不存在：{target}")

    payload = json.loads(target.read_text(encoding="utf-8"))

    items = payload.get("items")
    samples = payload.get("samples")

    if not isinstance(items, list) or not isinstance(samples, list):
        raise EvalError("夹具缺少 items / samples")

    if len(samples) != 60:
        raise EvalError(f"样本必须正好 60 条，实际 {len(samples)}")

    counts = {}

    for sample in samples:
        category = sample.get("category")

        if category not in CATEGORIES:
            raise EvalError(f"未知类别：{category!r}")

        counts[category] = counts.get(category, 0) + 1

    expected = {
        "preference": 20,
        "project_fact": 20,
        "conflict": 10,
        "deletion": 5,
        "unanswerable": 5,
    }

    if counts != expected:
        raise EvalError(f"样本配比不对：{counts} != {expected}")

    return payload


class MemoryEvaluator:
    def __init__(self, store: MemoryStore, dataset: dict):
        self.store = store
        self.dataset = dataset
        self.owner = dataset["owner_id"]
        self.versions = MemoryVersions(store)
        self._ids: dict[str, int] = {}

    # ---- 播种 -------------------------------------------------------

    def seed(self) -> dict[str, int]:
        ids = {}

        for spec in self.dataset["items"]:
            ref = spec["ref"]
            supersedes = spec.get("supersedes_ref")

            item = self.store.add_item(
                self.owner,
                kind=spec["kind"],
                layer=spec["layer"],
                trust=spec["trust"],
                key=spec["key"],
                text=spec["text"],
                conflict_group=spec.get("conflict_group", ""),
                valid_from=spec.get("valid_from"),
                valid_to=spec.get("valid_to"),
                supersedes_id=ids.get(supersedes) if supersedes else None,
            )

            ids[ref] = item.id

        self._ids = ids

        return ids

    # ---- 免费指标 ---------------------------------------------------

    def run(self) -> EvalReport:
        if not self._ids:
            self.seed()

        results: list[SampleResult] = []
        recall_hit = 0
        recall_total = 0
        conflict_correct = 0
        conflict_total = 0
        false_hits = 0
        unanswerable_total = 0

        snapshot = self.versions.create("eval-baseline")

        for sample in self.dataset["samples"]:
            category = sample["category"]

            if category == "deletion":
                continue  # 删除类单独一轮，避免影响召回统计

            response = self.store.search(self.owner, sample["query"], k=5)
            hits = response["hits"]
            top_keys = tuple(hit.item.key for hit in hits)

            if category in ("preference", "project_fact"):
                recall_total += 1
                passed = bool(set(top_keys) & set(sample["expected_keys"]))
                recall_hit += 1 if passed else 0
                detail = "" if passed else "top5 未命中期望条目"

            elif category == "conflict":
                recall_total += 1
                conflict_total += 1
                expected = set(sample["expected_keys"])
                hit5 = bool(set(top_keys) & expected)
                recall_hit += 1 if hit5 else 0
                top1_ok = bool(top_keys) and top_keys[0] in expected

                if top1_ok:
                    conflict_correct += 1

                resolution = resolve_group(
                    self.store, sample["conflict_group"], owner_id=self.owner
                )
                resolution_ok = (
                    resolution.item is not None
                    and resolution.item.key in expected
                )

                passed = top1_ok and resolution_ok
                detail = (
                    f"top1={top_keys[0] if top_keys else None} "
                    f"resolve={resolution.reason}"
                )

            else:  # unanswerable
                unanswerable_total += 1
                passed = not hits
                false_hits += len(hits)
                detail = "" if passed else f"硬答 {len(hits)} 条"

            results.append(
                SampleResult(
                    id=sample["id"],
                    category=category,
                    query=sample["query"],
                    passed=passed,
                    mode=response["mode"],
                    top_keys=top_keys,
                    detail=detail,
                )
            )

        deletion_total = 0
        resurrection = 0

        for sample in self.dataset["samples"]:
            if sample["category"] != "deletion":
                continue

            deletion_total += 1
            item_id = self._ids[sample["delete_key"]]

            before = self.store.search(self.owner, sample["query"], k=5)

            if not before["hits"]:
                results.append(
                    SampleResult(
                        id=sample["id"],
                        category="deletion",
                        query=sample["query"],
                        passed=False,
                        mode=before["mode"],
                        detail="删除前就检索不到，样本本身失效",
                    )
                )
                resurrection += 1
                continue

            self.store.delete_item(item_id)

            after = self.store.search(self.owner, sample["query"], k=5)
            still_there = any(
                hit.item.id == item_id for hit in after["hits"]
            ) or any(
                hit.item.key == sample["delete_key"] for hit in after["hits"]
            )

            if still_there:
                resurrection += 1

            results.append(
                SampleResult(
                    id=sample["id"],
                    category="deletion",
                    query=sample["query"],
                    passed=not still_there,
                    mode=after["mode"],
                    top_keys=tuple(hit.item.key for hit in after["hits"]),
                    detail="" if not still_there else "删除后仍能检索到",
                )
            )

        version_ok = self._check_version_rollback(snapshot.id)

        problems = []

        if recall_hit < RECALL_TARGET:
            problems.append(f"Recall@5 未达标：{recall_hit}/{recall_total} < {RECALL_TARGET}")

        if conflict_correct < CONFLICT_TARGET:
            problems.append(
                f"冲突解析未达标：{conflict_correct}/{conflict_total} < {CONFLICT_TARGET}"
            )

        if resurrection > RESURRECTION_TARGET:
            problems.append(f"删除后复活：{resurrection} 条 > 0")

        if false_hits > FALSE_HIT_TARGET:
            problems.append(f"不可回答样本被硬答：{false_hits} 条 > 0")

        if not version_ok:
            problems.append("版本回滚后检索结果不一致")

        return EvalReport(
            seeded=len(self._ids),
            recall_hit=recall_hit,
            recall_total=recall_total,
            conflict_correct=conflict_correct,
            conflict_total=conflict_total,
            resurrection=resurrection,
            deletion_total=deletion_total,
            false_hits=false_hits,
            unanswerable_total=unanswerable_total,
            version_rollback_ok=version_ok,
            results=tuple(results),
            problems=tuple(problems),
        )

    def _check_version_rollback(self, snapshot_id: int) -> bool:
        """回滚后，之前删掉的条目必须重新可检索。"""

        self.versions.restore(snapshot_id)

        probe_key = VERSION_PROBE_KEYS[0]
        probe_id = self._ids.get(probe_key)

        if probe_id is None:
            return False

        item = self.store.get_item(probe_id)

        if item is None or item.deleted_at is not None:
            return False

        hits = self.store.search(self.owner, "临时访问码", k=5)["hits"]

        return any(hit.item.id == probe_id for hit in hits)

    # ---- 付费档：无依据回答判定 -------------------------------------

    def build_answer_context(self, sample: dict) -> list[dict]:
        """取 top5 记忆作为回答上下文（含 id，供模型引用）。"""

        hits = self.store.search(self.owner, sample["query"], k=5)["hits"]

        return [
            {"id": hit.item.id, "text": hit.item.text} for hit in hits
        ]

    def run_answer_check(self, answerer) -> tuple[AnswerResult, ...]:
        """对每条样本让模型作答并判定"是否有依据"。

        `answerer(context, question) -> dict` 需返回
        `{"answer": str, "citations": [int], "insufficient": bool}`。
        判定只看：声称有依据却无引用、或引用不在上下文里。
        """

        results = []

        for sample in self.dataset["samples"]:
            context = self.build_answer_context(sample)
            allowed = {entry["id"] for entry in context}

            try:
                payload = answerer(context, sample["query"])

            except Exception as error:  # noqa: BLE001 - 记一条失败，不中断整轮
                results.append(
                    AnswerResult(
                        id=sample["id"],
                        category=sample["category"],
                        insufficient=True,
                        citations=(),
                        supported=False,
                        detail=f"作答失败：{type(error).__name__}",
                    )
                )
                continue

            citations = payload.get("citations") or []

            if not isinstance(citations, list):
                citations = []

            citations = tuple(
                int(item) for item in citations
                if isinstance(item, int) and not isinstance(item, bool)
            )
            insufficient = bool(payload.get("insufficient"))
            cites_valid = all(item in allowed for item in citations)

            unsupported = (not insufficient) and (not citations or not cites_valid)

            results.append(
                AnswerResult(
                    id=sample["id"],
                    category=sample["category"],
                    insufficient=insufficient,
                    citations=citations,
                    supported=not unsupported,
                    detail=""
                    if not unsupported
                    else ("无引用" if not citations else "引用了不存在的记忆"),
                )
            )

        return tuple(results)

    def attach_answers(self, report: EvalReport, answers) -> EvalReport:
        unsupported = sum(1 for item in answers if not item.supported)
        problems = list(report.problems)

        if unsupported > UNSUPPORTED_TARGET:
            problems.append(
                f"无依据回答 {unsupported} 条 > {UNSUPPORTED_TARGET}"
            )

        from dataclasses import replace

        return replace(
            report,
            answers=tuple(answers),
            unsupported=unsupported,
            answer_total=len(answers),
            problems=tuple(problems),
        )
