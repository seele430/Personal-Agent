"""ResearchGraph：五段状态机 + 两条有界回边。

    planned → researching → analyzing → writing ⇄ reviewing → done
                    ↑____________|          (E1 补研，最多 1 轮)
                                       
失败分支：failed（起手就定不了题 / 一条证据都没有）
部分完成：partial（评审到上限仍未过、预算耗尽）

两条回边各自的**硬上限**是这一层的核心，不是可调参数：

- **E1 补研**：`analyzing` 发现缺口，或本轮有搜索/抓取失败 → 回 `researching`，
  仅限 1 轮。补研只查缺口，**不重抓已有证据**（Evidence Store 的 SHA256
  去重保证同一个正文不会入库两次）。
- **E2 评审**：`reviewing` 判 revise 且重写次数 < 2 → 回 `writing`。
  连打两次仍不过 → `partial`，如实记下分数，**不降线凑过**。

每次迁移写一行 `research_events`（逐步持久化），所以"到底跑了几轮、
重写了几次"不是内存里的说法，而是库里的记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.research.llm import (
    LLMCall,
    LLMError,
    LLMInvalidResponse,
    LLMUsage,
    complete_json,
)
from src.research.prompts import (
    PASS_SCORE,
    Analysis,
    CriticResult,
    PlannerPlan,
    PromptContractError,
    WriterDraft,
    analyst_messages,
    build_evidence_digest,
    critic_messages,
    planner_messages,
    validate_analyst,
    validate_critic,
    validate_planner,
    validate_writer,
    writer_messages,
)
from src.research.store import (
    EventRecord,
    ResearchStore,
    get_research_store,
)


# ---- 状态 ----------------------------------------------------------------

PLANNED = "planned"
RESEARCHING = "researching"
ANALYZING = "analyzing"
WRITING = "writing"
REVIEWING = "reviewing"
REVISING = "revising"
DONE = "done"
PARTIAL = "partial"
FAILED = "failed"

STATES = frozenset(
    {
        PLANNED,
        RESEARCHING,
        ANALYZING,
        WRITING,
        REVIEWING,
        REVISING,
        DONE,
        PARTIAL,
        FAILED,
    }
)

MAX_LLM_CALLS = 16
MAX_RESEARCH_ROUNDS = 2
MAX_REVISIONS = 2
CONTRACT_RETRIES = 1
MAX_DIGEST_ITEMS = 12

STAGE_MAX_TOKENS = {
    "planner": 1024,
    "analyst": 3072,
    "writer": 4096,
    "critic": 1024,
}


class GraphError(RuntimeError):
    """图本身的失败（配置/预算），不是模型错误。"""


class BudgetExceeded(GraphError):
    """LLM 调用次数超预算。"""


@dataclass(frozen=True)
class SearchRoundResult:
    """一轮检索取证的结果。`failures` 是失败次数（搜索失败也算 1 次）。"""

    queries: tuple[str, ...]
    evidence_ids: tuple[int, ...]
    failures: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.evidence_ids)


@dataclass(frozen=True)
class GraphResult:
    run_id: int
    question: str
    state: str
    plan: PlannerPlan | None = None
    analysis: Analysis | None = None
    draft: WriterDraft | None = None
    critic: CriticResult | None = None
    research_rounds: int = 0
    revision_count: int = 0
    evidence_ids: tuple[int, ...] = ()
    search_failures: int = 0
    events: tuple[EventRecord, ...] = ()
    llm_calls: int = 0
    usage: LLMUsage = field(default_factory=LLMUsage)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.state == DONE


def _add_usage(left: LLMUsage, right: LLMUsage) -> LLMUsage:
    return LLMUsage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
    )


def default_llm(messages, *, stage: str, max_tokens: int):
    """默认调用：一次 `complete_json`，返回 (payload, LLMCall)。"""

    return complete_json(messages, max_tokens=max_tokens)


class ResearchGraph:
    """一张研究图。所有外部依赖都可注入，便于离线测试。"""

    def __init__(
        self,
        question: str,
        *,
        llm=None,
        search_round=None,
        evidence_store=None,
        store: ResearchStore | None = None,
        max_llm_calls: int = MAX_LLM_CALLS,
        max_research_rounds: int = MAX_RESEARCH_ROUNDS,
        max_revisions: int = MAX_REVISIONS,
        queries_per_round: int = 4,
        sources_per_query: int = 4,
        pages_per_query: int = 3,
        min_chars: int = 500,
        persist: bool = True,
    ):
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question 不能为空")

        for name, value, low, high in (
            ("max_llm_calls", max_llm_calls, 4, MAX_LLM_CALLS),
            ("max_research_rounds", max_research_rounds, 1, MAX_RESEARCH_ROUNDS),
            ("max_revisions", max_revisions, 0, MAX_REVISIONS),
            ("queries_per_round", queries_per_round, 1, 6),
            ("sources_per_query", sources_per_query, 1, 10),
            ("pages_per_query", pages_per_query, 1, 10),
            ("min_chars", min_chars, 1, 100_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} 必须是整数")

            if not low <= value <= high:
                raise ValueError(f"{name} 必须在 {low}..{high}")

        self.question = " ".join(question.split())
        self._llm = llm or default_llm
        self._search_round = search_round
        self._store = store if store is not None else get_research_store()
        self._evidence_store = evidence_store
        self._max_llm_calls = max_llm_calls
        self._max_research_rounds = max_research_rounds
        self._max_revisions = max_revisions
        self._queries_per_round = queries_per_round
        self._sources_per_query = sources_per_query
        self._pages_per_query = pages_per_query
        self._min_chars = min_chars
        self._persist = persist

        self._run_id = 0
        self._state = PLANNED
        self._seq = 0
        self._events: list[EventRecord] = []
        self._llm_calls = 0
        self._usage = LLMUsage()
        self._research_rounds = 0
        self._revision_count = 0
        self._evidence_ids: tuple[int, ...] = ()
        self._search_failures = 0

    # ---- 落库与迁移 --------------------------------------------------

    def _evidence(self):
        if self._evidence_store is None:
            from src.web.evidence import get_evidence_store

            self._evidence_store = get_evidence_store()

        return self._evidence_store

    def _transition(self, to_state: str, actor: str, reason: str) -> None:
        if to_state not in STATES:
            raise GraphError(f"未知状态：{to_state}")

        self._seq += 1

        from_state = self._state
        self._state = to_state

        if not self._persist:
            self._events.append(
                EventRecord(
                    run_id=self._run_id,
                    seq=self._seq,
                    from_state=from_state,
                    to_state=to_state,
                    actor=actor,
                    reason=reason[:300],
                    at="",
                )
            )
            return

        event = self._store.record_event(
            self._run_id,
            seq=self._seq,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            reason=reason,
        )

        self._store.update_run(self._run_id, state=to_state)
        self._events.append(event)

    # ---- 模型调用（带契约重试与预算） --------------------------------

    def _ask(self, stage: str, messages, validator, **kwargs):
        max_tokens = STAGE_MAX_TOKENS.get(stage, 1024)
        attempts = 0
        base_messages = list(messages)
        current = list(base_messages)

        def corrective(note: str):
            return list(base_messages) + [
                {
                    "role": "user",
                    "content": (
                        f"{note}"
                        "请只输出符合约定的 JSON 对象，不要任何解释。"
                    ),
                }
            ]

        while True:
            if self._llm_calls >= self._max_llm_calls:
                raise BudgetExceeded(
                    f"LLM 调用预算耗尽（上限 {self._max_llm_calls}）"
                )

            # 计数放在调用之前：失败的那次也算一次真实花费。
            self._llm_calls += 1

            try:
                payload, call = self._llm(
                    current, stage=stage, max_tokens=max_tokens
                )

            except LLMInvalidResponse as error:
                # 输出不是 JSON（常见于被 max_tokens 截断）：只重问一次，
                # 不猜它想写什么。
                attempts += 1

                if attempts > CONTRACT_RETRIES:
                    raise

                current = corrective(f"上一次输出不是合法 JSON（{error}）。")
                continue

            if isinstance(call, LLMCall):
                self._usage = _add_usage(self._usage, call.usage)

            try:
                return validator(payload, **kwargs)

            except PromptContractError as error:
                attempts += 1

                if attempts > CONTRACT_RETRIES:
                    raise

                current = corrective(f"上一次输出不符合约定：{error}。")

    # ---- 检索 --------------------------------------------------------

    def _search(self, queries: tuple[str, ...], round_index: int):
        if self._search_round is None:
            return self._default_search(queries, round_index)

        result = self._search_round(queries, round_index)

        if not isinstance(result, SearchRoundResult):
            raise GraphError("search_round 必须返回 SearchRoundResult")

        return result

    def _default_search(self, queries, round_index):
        from src.web.pipeline import run_research

        store = self._evidence()
        evidence_ids: list[int] = []
        failures = 0
        ran = []

        for query in queries[: self._queries_per_round]:
            run = run_research(
                query,
                store=store,
                count=self._sources_per_query,
                max_pages=self._pages_per_query,
                min_chars=self._min_chars,
            )

            ran.append(run.query)
            failures += 1 if run.error else 0
            failures += len(run.failures)

            for evidence_id in run.evidence_ids:
                if evidence_id not in evidence_ids:
                    evidence_ids.append(evidence_id)

        return SearchRoundResult(
            queries=tuple(ran),
            evidence_ids=tuple(evidence_ids),
            failures=failures,
        )

    # ---- 证据摘要 ----------------------------------------------------

    def _digest(self, evidence_ids: tuple[int, ...], *, limit: int = MAX_DIGEST_ITEMS):
        store = self._evidence()
        records = []
        texts = {}

        for evidence_id in evidence_ids[:limit]:
            record = store.get_source(evidence_id)

            if record is None:
                continue

            records.append(
                {
                    "id": record.id,
                    "title": record.title,
                    "url": record.url,
                }
            )

            try:
                texts[record.id] = store.get_text(record.id)

            except Exception:  # noqa: BLE001 - 缺正文不该让整条流程挂掉
                texts[record.id] = ""

        return build_evidence_digest(records, texts)

    # ---- 主流程 ------------------------------------------------------

    def run(self) -> GraphResult:
        self._run_id = self._store.create_run(self.question) if self._persist else 0

        try:
            return self._run_inner()

        except BudgetExceeded as error:
            return self._finish(PARTIAL, str(error))

        except (LLMError, PromptContractError) as error:
            return self._finish(FAILED, f"模型调用失败：{error}")

    def _run_inner(self) -> GraphResult:
        # 1. Planner
        plan = self._ask(
            "planner",
            planner_messages(self.question, max_queries=self._queries_per_round),
            validate_planner,
            question=self.question,
        )

        # 2. 第一轮取证
        self._transition(
            RESEARCHING, "planner", f"子查询 {len(plan.sub_queries)} 条"
        )

        queries = tuple(item.query for item in plan.sub_queries)
        round_one = self._search(queries, 1)

        self._research_rounds = 1
        self._search_failures = round_one.failures
        self._evidence_ids = round_one.evidence_ids

        if not self._evidence_ids:
            return self._finish(FAILED, "一条证据都没抓到，无法成文")

        # 3. 分析
        self._transition(ANALYZING, "researcher", "首轮分析")
        analysis = self._analyze()

        # 4. E1 补研回边（最多 1 轮）
        while (
            (analysis.has_gaps or self._search_failures > 0)
            and self._research_rounds < self._max_research_rounds
        ):
            reason = (
                f"缺口 {len(analysis.gaps)} 个 / 失败 {self._search_failures} 次"
            )
            self._transition(RESEARCHING, "analyst", f"补研：{reason}")

            gap_queries = tuple(item.query for item in analysis.gaps)

            if not gap_queries:
                gap_queries = queries[: self._queries_per_round]

            extra = self._search(gap_queries, self._research_rounds + 1)

            self._research_rounds += 1
            self._search_failures += extra.failures

            merged = list(self._evidence_ids)

            for evidence_id in extra.evidence_ids:
                if evidence_id not in merged:
                    merged.append(evidence_id)

            self._evidence_ids = tuple(merged)

            self._transition(ANALYZING, "researcher", "补研后重新分析")
            analysis = self._analyze()

        # 5. 写作
        self._transition(WRITING, "analyst", "分析完成，开始成文")

        draft = self._write(
            [f"{item.claim}（证据 {item.evidence_ids}）" for item in analysis.findings],
            analysis,
        )

        # 6. 评审 ⇄ 重写（E2 回边）
        while True:
            self._transition(REVIEWING, "writer", "提交评审")
            critic = self._review(draft)

            if self._persist:
                self._store.update_run(self._run_id, critic_score=critic.score)

            if critic.passed:
                self._transition(DONE, "critic", f"评审通过（{critic.score} 分）")

                return self._finish(
                    DONE, "", draft=draft, analysis=analysis, plan=plan, critic=critic
                )

            if self._revision_count >= self._max_revisions:
                return self._finish(
                    PARTIAL,
                    f"评审到上限仍未通过（{critic.score} 分，{len(critic.issues)} 条意见）",
                    draft=draft,
                    analysis=analysis,
                    plan=plan,
                    critic=critic,
                )

            self._revision_count += 1
            self._transition(
                REVISING,
                "critic",
                f"第 {self._revision_count} 次重写（{critic.score} 分）",
            )

            notes = [
                issue.action or issue.reason for issue in critic.issues
            ] or ["按评审意见提升证据支撑与表达"]

            draft = self._write(notes, analysis)
            self._transition(WRITING, "writer", "重写完成")

    def _analyze(self) -> Analysis:
        digest = self._digest(self._evidence_ids)

        return self._ask(
            "analyst",
            analyst_messages(self.question, digest),
            validate_analyst,
        )

    def _write(self, notes: list[str], analysis: Analysis) -> WriterDraft:
        digest = self._digest(self._evidence_ids)

        findings = [
            {"claim": item.claim, "evidence_ids": list(item.evidence_ids)}
            for item in analysis.findings
        ]

        return self._ask(
            "writer",
            writer_messages(
                self.question,
                findings=findings,
                evidence=digest,
                revision_notes=notes,
            ),
            validate_writer,
        )

    def _review(self, draft: WriterDraft) -> CriticResult:
        return self._ask(
            "critic",
            critic_messages(
                self.question,
                draft={
                    "title": draft.title,
                    "summary": draft.summary,
                    "sections": [
                        {
                            "heading": section.heading,
                            "paragraphs": [
                                {
                                    "text": paragraph.text,
                                    "evidence_ids": list(paragraph.evidence_ids),
                                }
                                for paragraph in section.paragraphs
                            ],
                        }
                        for section in draft.sections
                    ],
                    "limitations": draft.limitations,
                },
                evidence=[],
            ),
            validate_critic,
        )

    def _finish(
        self,
        state: str,
        error: str,
        *,
        draft=None,
        analysis=None,
        plan=None,
        critic=None,
    ) -> GraphResult:
        if self._state != state:
            self._transition(state, "graph", error or "结束")

        if self._persist:
            self._store.update_run(
                self._run_id,
                state=state,
                research_rounds=self._research_rounds,
                revision_count=self._revision_count,
                evidence_ids=self._evidence_ids,
                error=error,
            )

        return GraphResult(
            run_id=self._run_id,
            question=self.question,
            state=state,
            plan=plan,
            analysis=analysis,
            draft=draft,
            critic=critic,
            research_rounds=self._research_rounds,
            revision_count=self._revision_count,
            evidence_ids=self._evidence_ids,
            search_failures=self._search_failures,
            events=tuple(self._events),
            llm_calls=self._llm_calls,
            usage=self._usage,
            error=error,
        )


def run_graph(question: str, **kwargs) -> GraphResult:
    """一次性入口：建图并跑完。"""

    return ResearchGraph(question, **kwargs).run()
