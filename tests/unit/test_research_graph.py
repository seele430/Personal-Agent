"""ResearchGraph 状态机测试：两条回边、上限、预算、落库。

全部离线：LLM、检索、证据库、账本都可注入或在临时目录里。
"""

import json
import tempfile
import unittest

from pathlib import Path

from src.research.graph import (
    ANALYZING,
    DONE,
    FAILED,
    PARTIAL,
    RESEARCHING,
    REVISING,
    REVIEWING,
    WRITING,
    ResearchGraph,
    SearchRoundResult,
)
from src.research.llm import LLMCall, LLMUsage
from src.research.store import ResearchStore
from src.web.evidence import EvidenceStore


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "research"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


PLANNER = load("planner.json")
ANALYST_GAP = load("analyst_gap.json")
ANALYST_OK = load("analyst_ok.json")
WRITER = load("writer.json")
CRITIC_REVISE = load("critic_revise.json")
CRITIC_PASS = load("critic_pass.json")


class FakeLLM:
    """按阶段脚本返回 payload；记录每次调用的阶段与消息。"""

    def __init__(self, script):
        self.script = {stage: list(items) for stage, items in script.items()}
        self.calls = []

    def __call__(self, messages, *, stage, max_tokens):
        self.calls.append({"stage": stage, "messages": messages, "max_tokens": max_tokens})

        queue = self.script.get(stage)

        if not queue:
            raise AssertionError(f"阶段 {stage} 没有更多脚本响应")

        item = queue.pop(0)

        if isinstance(item, Exception):
            raise item

        return item, LLMCall(
            text="{}", usage=LLMUsage(5, 5, 10), latency_ms=1.0, attempts=1,
            model="fake",
        )

    def stages(self):
        return [call["stage"] for call in self.calls]


class FakeSearch:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, queries, round_index):
        self.calls.append((tuple(queries), round_index))
        return self.results[len(self.calls) - 1]


class GraphTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)

        self.evidence = EvidenceStore(root / "evidence.db")
        self.store = ResearchStore(root / "research.db")

        self.evidence.add_source(
            url="https://arxiv.org/abs/2309.06180",
            text="PagedAttention stores the KV cache in non-contiguous blocks.",
            title="PagedAttention",
        )
        self.evidence.add_source(
            url="https://docs.vllm.ai/en/latest/",
            text="vLLM is a serving engine built on PagedAttention.",
            title="vLLM Docs",
        )

    def graph(self, llm, search, **kwargs):
        return ResearchGraph(
            "PagedAttention 如何管理显存",
            llm=llm,
            search_round=search,
            evidence_store=self.evidence,
            store=self.store,
            **kwargs,
        )

    def round_result(self, ids=(1, 2), failures=0):
        return SearchRoundResult(
            queries=("q",), evidence_ids=tuple(ids), failures=failures
        )


class TestHappyPath(GraphTestCase):
    def test_full_run_reaches_done(self):
        llm = FakeLLM(
            {
                "planner": [PLANNER],
                "analyst": [ANALYST_OK],
                "writer": [WRITER],
                "critic": [CRITIC_PASS],
            }
        )
        search = FakeSearch([self.round_result()])

        result = self.graph(llm, search).run()

        self.assertEqual(result.state, DONE)
        self.assertEqual(result.research_rounds, 1)
        self.assertEqual(result.revision_count, 0)
        self.assertEqual(result.llm_calls, 4)
        self.assertEqual(len(result.evidence_ids), 2)
        self.assertEqual(
            [event.to_state for event in result.events],
            [RESEARCHING, ANALYZING, WRITING, REVIEWING, DONE],
        )
        self.assertEqual(llm.stages(), ["planner", "analyst", "writer", "critic"])

    def test_writer_gets_findings_and_evidence_in_prompt(self):
        llm = FakeLLM(
            {
                "planner": [PLANNER],
                "analyst": [ANALYST_OK],
                "writer": [WRITER],
                "critic": [CRITIC_PASS],
            }
        )

        self.graph(llm, FakeSearch([self.round_result()])).run()

        writer_prompt = next(
            call["messages"][1]["content"]
            for call in llm.calls
            if call["stage"] == "writer"
        )

        self.assertIn("PagedAttention", writer_prompt)
        self.assertIn("<evidence", writer_prompt)
        self.assertIn("至少 3000 字", writer_prompt)


class TestBackEdgeResearch(GraphTestCase):
    def test_gap_triggers_exactly_one_extra_round(self):
        llm = FakeLLM(
            {
                "planner": [PLANNER],
                "analyst": [ANALYST_GAP, ANALYST_OK],
                "writer": [WRITER],
                "critic": [CRITIC_PASS],
            }
        )
        search = FakeSearch([self.round_result(), self.round_result()])

        result = self.graph(llm, search).run()

        self.assertEqual(result.state, DONE)
        self.assertEqual(result.research_rounds, 2)
        self.assertEqual(len(search.calls), 2)
        self.assertEqual(
            search.calls[1][0], ("vLLM production memory usage comparison benchmark",)
        )
        self.assertTrue(
            any("补研" in event.reason for event in result.events),
            "回边原因里应写明是补研",
        )

    def test_search_failure_also_triggers_back_edge(self):
        llm = FakeLLM(
            {
                "planner": [PLANNER],
                "analyst": [ANALYST_OK, ANALYST_OK],
                "writer": [WRITER],
                "critic": [CRITIC_PASS],
            }
        )
        search = FakeSearch(
            [self.round_result(failures=2), self.round_result()]
        )

        result = self.graph(llm, search).run()

        self.assertEqual(result.research_rounds, 2)
        self.assertEqual(result.search_failures, 2)
        self.assertEqual(len(search.calls), 2)

    def test_back_edge_is_capped_at_two_rounds(self):
        llm = FakeLLM(
            {
                "planner": [PLANNER],
                "analyst": [ANALYST_GAP, ANALYST_GAP, ANALYST_GAP],
                "writer": [WRITER],
                "critic": [CRITIC_PASS],
            }
        )
        search = FakeSearch(
            [self.round_result(), self.round_result(), self.round_result()]
        )

        result = self.graph(llm, search).run()

        self.assertEqual(result.research_rounds, 2)
        self.assertEqual(len(search.calls), 2, "补研最多 1 轮，不许再回边")
        self.assertEqual(result.state, DONE)


class TestBackEdgeRevision(GraphTestCase):
    def test_two_revisions_then_pass(self):
        llm = FakeLLM(
            {
                "planner": [PLANNER],
                "analyst": [ANALYST_OK],
                "writer": [WRITER, WRITER, WRITER],
                "critic": [CRITIC_REVISE, CRITIC_REVISE, CRITIC_PASS],
            }
        )

        result = self.graph(llm, FakeSearch([self.round_result()])).run()

        self.assertEqual(result.state, DONE)
        self.assertEqual(result.revision_count, 2)
        self.assertEqual(llm.stages().count("writer"), 3)
        self.assertEqual(llm.stages().count("critic"), 3)
        revising = [
            event for event in result.events if event.to_state == REVISING
        ]
        self.assertEqual([event.seq for event in revising], sorted(e.seq for e in revising))

    def test_revision_cap_yields_partial_not_pass(self):
        llm = FakeLLM(
            {
                "planner": [PLANNER],
                "analyst": [ANALYST_OK],
                "writer": [WRITER, WRITER, WRITER],
                "critic": [CRITIC_REVISE] * 3,
            }
        )

        result = self.graph(llm, FakeSearch([self.round_result()])).run()

        self.assertEqual(result.state, PARTIAL)
        self.assertEqual(result.revision_count, 2)
        self.assertEqual(llm.stages().count("writer"), 3)
        self.assertIn("未通过", result.error)
        self.assertEqual(result.critic.score, 6)

    def test_revision_notes_reach_writer(self):
        llm = FakeLLM(
            {
                "planner": [PLANNER],
                "analyst": [ANALYST_OK],
                "writer": [WRITER, WRITER],
                "critic": [CRITIC_REVISE, CRITIC_PASS],
            }
        )

        self.graph(llm, FakeSearch([self.round_result()])).run()

        second_writer = [
            call["messages"][1]["content"]
            for call in llm.calls
            if call["stage"] == "writer"
        ][1]

        self.assertIn("修订稿", second_writer)
        self.assertIn("补充论文的硬件与负载条件", second_writer)


class TestFailuresAndBudgets(GraphTestCase):
    def test_contract_error_is_retried_once(self):
        bad_planner = dict(PLANNER)
        bad_planner["sub_queries"] = PLANNER["sub_queries"][:2]

        llm = FakeLLM(
            {
                "planner": [bad_planner, PLANNER],
                "analyst": [ANALYST_OK],
                "writer": [WRITER],
                "critic": [CRITIC_PASS],
            }
        )

        result = self.graph(llm, FakeSearch([self.round_result()])).run()

        self.assertEqual(result.state, DONE)
        self.assertEqual(llm.stages().count("planner"), 2)

    def test_contract_error_twice_fails(self):
        bad_planner = dict(PLANNER)
        bad_planner["sub_queries"] = []

        llm = FakeLLM({"planner": [bad_planner, bad_planner]})

        result = self.graph(llm, FakeSearch([self.round_result()])).run()

        self.assertEqual(result.state, FAILED)
        self.assertIn("模型调用失败", result.error)

    def test_no_evidence_fails_fast(self):
        llm = FakeLLM({"planner": [PLANNER]})

        result = self.graph(
            llm, FakeSearch([self.round_result(ids=())])
        ).run()

        self.assertEqual(result.state, FAILED)
        self.assertIn("一条证据", result.error)
        self.assertEqual(llm.stages(), ["planner"], "没有证据就不该继续烧模型")

    def test_budget_exhaustion_is_partial(self):
        llm = FakeLLM(
            {
                "planner": [PLANNER],
                "analyst": [ANALYST_OK],
                "writer": [WRITER, WRITER],
                "critic": [CRITIC_REVISE, CRITIC_REVISE],
            }
        )

        result = self.graph(
            llm, FakeSearch([self.round_result()]), max_llm_calls=4
        ).run()

        self.assertEqual(result.state, PARTIAL)
        self.assertIn("预算", result.error)
        self.assertEqual(result.llm_calls, 4)

    def test_empty_question_and_bad_limits(self):
        llm = FakeLLM({})
        search = FakeSearch([])

        with self.assertRaises(ValueError):
            ResearchGraph("   ", llm=llm, store=self.store)

        for kwargs in (
            {"max_llm_calls": 1},
            {"max_research_rounds": 3},
            {"max_revisions": 5},
            {"queries_per_round": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ResearchGraph("q", llm=llm, store=self.store, **kwargs)


class TestPersistence(GraphTestCase):
    def test_run_and_events_are_persisted(self):
        llm = FakeLLM(
            {
                "planner": [PLANNER],
                "analyst": [ANALYST_GAP, ANALYST_OK],
                "writer": [WRITER, WRITER],
                "critic": [CRITIC_REVISE, CRITIC_PASS],
            }
        )
        search = FakeSearch([self.round_result(), self.round_result()])

        result = self.graph(llm, search).run()

        run = self.store.get_run(result.run_id)

        self.assertEqual(run.state, DONE)
        self.assertEqual(run.research_rounds, 2)
        self.assertEqual(run.revision_count, 1)
        self.assertEqual(run.critic_score, 9)
        self.assertEqual(run.evidence_ids, (1, 2))
        self.assertEqual(run.question, "PagedAttention 如何管理显存")

        events = self.store.events(result.run_id)

        self.assertEqual(len(events), len(result.events))
        self.assertEqual(
            [event.seq for event in events],
            list(range(1, len(events) + 1)),
        )
        self.assertTrue(all(event.actor for event in events))

    def test_partial_is_persisted_with_score(self):
        llm = FakeLLM(
            {
                "planner": [PLANNER],
                "analyst": [ANALYST_OK],
                "writer": [WRITER, WRITER, WRITER],
                "critic": [CRITIC_REVISE] * 3,
            }
        )

        result = self.graph(llm, FakeSearch([self.round_result()])).run()
        run = self.store.get_run(result.run_id)

        self.assertEqual(run.state, PARTIAL)
        self.assertEqual(run.critic_score, 6)
        self.assertIn("未通过", run.error)


if __name__ == "__main__":
    unittest.main()
