"""检索注入与引用（R4）的测试。

覆盖三件容易出事的事：

1. **空检索不注入**：没有命中时 `text` 为空，system prompt 里也不会
   出现"要求引用"的指令；
2. **编号可回溯**：注入块里的 `[n]` 与 `chunk_id` 一一对应，
   截断之后仍然对得上（审计记的是真正注入的片段，不是检索到的前 k 条）；
3. **身份是硬门槛**：没有登录身份就不检索（绝不回退到默认用户），
   检索失败也不影响这一轮聊天。

模型调用全部被 patch：不联网、不花钱。
"""

import unittest

from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from tests.helper import isolate_paths
from src import agent
from src.core.reminder_identity import (
    bind_authenticated_reminder,
)
from src.knowledge import KnowledgeStore, ingest_upload
from src.knowledge.inject import (
    CITATION_RULE,
    build_block,
    build_retrieval_context,
    estimate_tokens,
    map_citations,
    parse_citations,
)


DEFAULT_TEXT = "# 性能\n\n用 cProfile 分析 asyncio 的瓶颈与协程调度。"


def hit(chunk_id, text, *, document_id=1, title=None, url=None):
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "seq": 0,
        "text": text,
        "title": title,
        "url": url,
        "score": 0.0,
        "rank": 1,
    }


class TestBudget(unittest.TestCase):
    """注入预算：宁可少注入，绝不超预算。"""

    def test_token_estimate_is_language_aware(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("中文"), 2)
        self.assertEqual(estimate_tokens("abcdefgh"), 2)

    def test_empty_hits_inject_nothing(self):
        block = build_block([])

        self.assertEqual(block["text"], "")
        self.assertEqual(block["used"], [])
        self.assertEqual(block["omitted"], 0)

    def test_numbering_matches_order(self):
        block = build_block([
            hit(11, "第一段内容"),
            hit(22, "第二段内容"),
        ])

        self.assertEqual(
            [item["number"] for item in block["used"]],
            [1, 2],
        )
        self.assertEqual(
            [item["chunk_id"] for item in block["used"]],
            [11, 22],
        )
        self.assertIn("[1]", block["text"])
        self.assertIn("[2]", block["text"])

    def test_budget_drops_tail_and_says_so(self):
        hits = [
            hit(number, "很长的一段内容" * 40)
            for number in range(1, 6)
        ]

        block = build_block(hits, max_tokens=120)

        self.assertLess(len(block["used"]), len(hits))
        self.assertGreater(block["omitted"], 0)
        self.assertIn("未展示", block["text"])
        self.assertLessEqual(
            estimate_tokens(block["text"]), 120
        )

    def test_single_huge_hit_is_truncated(self):
        block = build_block(
            [hit(7, "啊" * 500)],
            max_tokens=80,
        )

        self.assertEqual(len(block["used"]), 1)
        self.assertTrue(block["truncated"])
        self.assertIn("截断", block["text"])
        self.assertLessEqual(
            estimate_tokens(block["text"]), 80
        )

    def test_impossible_budget_injects_nothing(self):
        block = build_block([hit(1, "内容")], max_tokens=1)

        self.assertEqual(block["text"], "")

    def test_invalid_budget_is_rejected(self):
        with self.assertRaises(ValueError):
            build_block([], max_tokens=0)


class TestCitation(unittest.TestCase):
    """引用解析：只认真实出现过的编号。"""

    def test_parse_keeps_only_valid_unique_numbers(self):
        self.assertEqual(
            parse_citations("见 [2]、[9] 和 [2]", 3),
            [2],
        )

    def test_parse_rejects_non_text(self):
        self.assertEqual(parse_citations(None, 3), [])
        self.assertEqual(parse_citations("", 3), [])
        self.assertEqual(parse_citations("[1]", 0), [])

    def test_map_citations_returns_chunk_ids(self):
        hits = [hit(11, "a"), hit(22, "b")]

        self.assertEqual(
            map_citations("先看 [2]，再看 [1]。", hits),
            [22, 11],
        )
        self.assertEqual(
            map_citations("完全没有引用", hits), []
        )

    def test_chinese_brackets_are_not_citations(self):
        hits = [hit(11, "a")]

        self.assertEqual(
            map_citations("【1】只是正文里的方括号", hits),
            [],
        )


class TestRetrievalContext(unittest.TestCase):
    """检索 → 组块 → 审计落库。"""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        isolate_paths(self, self.temp)

        self.store = KnowledgeStore(
            self.temp.name + "/kb.db"
        )

    def ingest(self, owner="alice", text=None, name="a"):
        return ingest_upload(
            owner_id=owner,
            filename=name,
            text=text or DEFAULT_TEXT,
            store=self.store,
        )

    def test_empty_store_audits_but_injects_nothing(self):
        context = build_retrieval_context(
            owner_id="alice",
            query="asyncio 瓶颈",
            store=self.store,
        )

        self.assertTrue(context.empty)
        self.assertEqual(context.text, "")
        self.assertIsNotNone(context.retrieval_id)

        audit = self.store.list_retrievals("alice")[0]

        self.assertEqual(audit["top_chunk_ids"], [])
        self.assertEqual(audit["cited_chunk_ids"], [])

    def test_numbering_and_audit_agree(self):
        self.ingest()

        context = build_retrieval_context(
            owner_id="alice",
            query="asyncio 瓶颈",
            store=self.store,
        )

        self.assertFalse(context.empty)
        self.assertEqual(len(context.hits), 1)
        self.assertEqual(context.hits[0]["number"], 1)
        self.assertIn("[1]", context.text)

        audit = self.store.list_retrievals("alice")[0]

        self.assertEqual(
            audit["top_chunk_ids"],
            [context.hits[0]["chunk_id"]],
        )

    def test_truncated_context_only_audits_injected_chunks(self):
        for index in range(4):
            self.ingest(
                name=f"note-{index}",
                text=(
                    "# Python 性能\n\n"
                    + "asyncio 瓶颈与事件循环阻塞点。" * 20
                ),
            )

        context = build_retrieval_context(
            owner_id="alice",
            query="asyncio 瓶颈",
            store=self.store,
            k=4,
            max_tokens=120,
        )

        audit = self.store.list_retrievals("alice")[0]

        # 审计里的编号必须与真正注入的片段一致，
        # 否则模型引用的 [1] 会指到一个没进上下文的片段。
        self.assertEqual(
            audit["top_chunk_ids"],
            [item["chunk_id"] for item in context.hits],
        )
        self.assertLess(
            len(audit["top_chunk_ids"]), 4
        )

    def test_citations_are_written_back(self):
        self.ingest()

        context = build_retrieval_context(
            owner_id="alice",
            query="asyncio 瓶颈",
            store=self.store,
        )

        chunk_id = context.hits[0]["chunk_id"]

        self.assertEqual(
            context.record_citations("结论见 [1]。"),
            [chunk_id],
        )

        audit = self.store.list_retrievals("alice")[0]

        self.assertEqual(
            audit["cited_chunk_ids"], [chunk_id]
        )

    def test_unknown_citation_leaves_audit_empty(self):
        self.ingest()

        context = build_retrieval_context(
            owner_id="alice",
            query="asyncio 瓶颈",
            store=self.store,
        )

        self.assertEqual(
            context.record_citations("我不确定，[9] 也许是。"),
            [],
        )

        audit = self.store.list_retrievals("alice")[0]

        self.assertEqual(audit["cited_chunk_ids"], [])

    def test_citations_trace_back_to_document(self):
        document = self.ingest()

        context = build_retrieval_context(
            owner_id="alice",
            query="asyncio 瓶颈",
            store=self.store,
        )

        chunk_id = context.hits[0]["chunk_id"]

        self.assertEqual(
            self.store.chunk_documents([chunk_id]),
            {chunk_id: document["id"]},
        )

    def test_other_owner_sees_nothing(self):
        self.ingest(owner="alice")

        context = build_retrieval_context(
            owner_id="bob",
            query="asyncio 瓶颈",
            store=self.store,
        )

        self.assertTrue(context.empty)

        self.assertEqual(
            self.store.list_retrievals("bob")[0][
                "top_chunk_ids"
            ],
            [],
        )
        self.assertEqual(
            self.store.list_retrievals("alice"), []
        )


class TestAgentInjection(unittest.TestCase):
    """chat() 侧的注入：身份门槛、空检索不注入、失败不炸聊天。"""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        isolate_paths(self, self.temp)

        self.init_patch = patch.object(agent, "init_db")
        self.init_patch.start()
        self.addCleanup(self.init_patch.stop)

    @staticmethod
    def reply(text):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=text,
                        tool_calls=None,
                    ),
                ),
            ],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            ),
        )

    def ingest(self, owner="alice"):
        return ingest_upload(
            owner_id=owner,
            filename="notes",
            text=DEFAULT_TEXT,
            store=KnowledgeStore(),
        )

    def test_without_identity_no_retrieval(self):
        with patch.object(
            agent, "build_retrieval_context"
        ) as build:
            with patch.object(
                agent.client.chat.completions,
                "create",
                return_value=self.reply("你好呀"),
            ) as model:
                self.assertEqual(
                    agent.chat("你好"), "你好呀"
                )

        build.assert_not_called()

        messages = model.call_args.kwargs["messages"]

        self.assertEqual(len(messages), 2)
        self.assertNotIn(
            CITATION_RULE, messages[0]["content"]
        )

    def test_with_identity_injects_and_records_citation(self):
        self.ingest()

        with bind_authenticated_reminder(
            user_id="alice",
            request_id="req_" + "a" * 12,
            role="owner",
        ):
            with patch.object(
                agent.client.chat.completions,
                "create",
                return_value=self.reply(
                    "瓶颈在事件循环 [1]"
                ),
            ) as model:
                agent.chat("asyncio 瓶颈")

        messages = model.call_args.kwargs["messages"]

        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn(
            CITATION_RULE, messages[0]["content"]
        )
        self.assertEqual(messages[1]["role"], "system")
        self.assertIn("[1]", messages[1]["content"])
        self.assertEqual(messages[2]["role"], "user")

        audit = KnowledgeStore().list_retrievals(
            "alice"
        )[0]

        self.assertTrue(audit["top_chunk_ids"])
        self.assertEqual(
            audit["cited_chunk_ids"],
            audit["top_chunk_ids"],
        )

    def test_empty_retrieval_does_not_inject(self):
        with bind_authenticated_reminder(
            user_id="alice",
            request_id="req_empty00001",
            role="owner",
        ):
            with patch.object(
                agent.client.chat.completions,
                "create",
                return_value=self.reply("没问题"),
            ) as model:
                agent.chat("你还记得那篇文章吗")

        messages = model.call_args.kwargs["messages"]

        self.assertEqual(len(messages), 2)
        self.assertNotIn(
            CITATION_RULE, messages[0]["content"]
        )

        # 空检索也要留下审计：检索确实发生过。
        audit = KnowledgeStore().list_retrievals(
            "alice"
        )[0]

        self.assertEqual(audit["top_chunk_ids"], [])

    def test_retrieval_failure_keeps_chat_alive(self):
        with bind_authenticated_reminder(
            user_id="alice",
            request_id="req_broken0001",
            role="owner",
        ):
            with patch.object(
                agent,
                "build_retrieval_context",
                side_effect=OSError("模拟知识库不可用"),
            ):
                with patch.object(
                    agent.logger, "exception"
                ) as log:
                    with patch.object(
                        agent.client.chat.completions,
                        "create",
                        return_value=self.reply(
                            "照常回复"
                        ),
                    ) as model:
                        self.assertEqual(
                            agent.chat("你好"),
                            "照常回复",
                        )

        self.assertEqual(
            len(model.call_args.kwargs["messages"]), 2
        )
        log.assert_called_once()


if __name__ == "__main__":
    unittest.main()
