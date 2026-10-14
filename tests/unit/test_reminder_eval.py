"""提醒 eval 判定逻辑的单元测试。

不调用模型，不访问网络，不使用数据库。
"""

import json
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.evals.reminder_eval import (
    EvalCase,
    ToolCall,
    extract_tool_calls,
    grade,
    load_cases,
    parse_case,
)


def tool_call(minutes, message="提醒内容", name="create_reminder"):
    return ToolCall(
        name=name,
        arguments=json.dumps(
            {"message": message, "minutes": minutes},
            ensure_ascii=False,
        ),
    )


CALL_CASE = EvalCase(
    id="rel_01",
    category="relative",
    input="10分钟后提醒我关火",
    expect="call",
    minutes=10,
    keywords=("关火",),
)

NO_CALL_CASE = EvalCase(
    id="none_01",
    category="no_call",
    input="你好",
    expect="no_call",
)

GUARD_CASE = EvalCase(
    id="guard_01",
    category="guard",
    input="连续创建 5 个提醒",
    expect="at_most_one",
)

NO_MUTATION_CASE = EvalCase(
    id="chat_04",
    category="other_intent",
    input="我现在有哪些提醒？",
    expect="no_mutation",
)


def list_call():
    """只读的 list_reminders 调用。"""

    return ToolCall(
        name="list_reminders",
        arguments="{}",
    )


def cancel_call(reminder_id=7):
    return ToolCall(
        name="cancel_reminder",
        arguments=json.dumps(
            {"reminder_id": reminder_id},
        ),
    )


def update_call(reminder_id=7):
    return ToolCall(
        name="update_reminder",
        arguments=json.dumps(
            {"reminder_id": reminder_id, "minutes": 30},
        ),
    )


class TestReminderEval(unittest.TestCase):

    # ---------- parse_case ----------

    def test_parse_valid_call_case(self):
        case = parse_case({
            "id": "rel_01",
            "category": "relative",
            "input": "10分钟后提醒我关火",
            "expect": "call",
            "minutes": 10,
            "keywords": ["关火"],
        })

        self.assertEqual(case, CALL_CASE)

    def test_call_case_requires_minutes(self):
        with self.assertRaises(ValueError):
            parse_case({
                "id": "x",
                "category": "c",
                "input": "提醒我",
                "expect": "call",
            })

    def test_no_call_case_rejects_minutes(self):
        with self.assertRaises(ValueError):
            parse_case({
                "id": "x",
                "category": "c",
                "input": "你好",
                "expect": "no_call",
                "minutes": 5,
            })

    def test_unknown_expect_rejected(self):
        with self.assertRaises(ValueError):
            parse_case({
                "id": "x",
                "category": "c",
                "input": "你好",
                "expect": "maybe",
            })

    # ---------- load_cases ----------

    def write_cases(self, lines):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)

        path = Path(temp.name) / "cases.jsonl"
        # 带 BOM，模拟 PowerShell 写入
        path.write_text("\n".join(lines), encoding="utf-8-sig")
        return path

    def test_load_skips_blank_lines(self):
        path = self.write_cases([
            json.dumps({
                "id": "a", "category": "c",
                "input": "你好", "expect": "no_call",
            }),
            "",
            json.dumps({
                "id": "b", "category": "c",
                "input": "在吗", "expect": "no_call",
            }),
        ])

        self.assertEqual(
            [case.id for case in load_cases(path)],
            ["a", "b"],
        )

    def test_load_rejects_duplicate_ids(self):
        line = json.dumps({
            "id": "a", "category": "c",
            "input": "你好", "expect": "no_call",
        })

        with self.assertRaises(ValueError):
            load_cases(self.write_cases([line, line]))

    # ---------- grade: call ----------

    def test_call_passes(self):
        result = grade(CALL_CASE, [tool_call(10, "记得关火")])
        self.assertTrue(result.passed, result.reason)

    def test_wrong_minutes_fails(self):
        self.assertFalse(
            grade(CALL_CASE, [tool_call(15, "关火")]).passed
        )

    def test_tolerance_allows_rounding(self):
        case = EvalCase(
            id="abs", category="absolute", input="x",
            expect="call", minutes=180, tolerance=1,
        )

        self.assertTrue(grade(case, [tool_call(181)]).passed)
        self.assertFalse(grade(case, [tool_call(182)]).passed)

    def test_missing_keyword_fails(self):
        self.assertFalse(
            grade(CALL_CASE, [tool_call(10, "喝水")]).passed
        )

    def test_bool_minutes_rejected(self):
        self.assertFalse(
            grade(CALL_CASE, [tool_call(True, "关火")]).passed
        )

    def test_invalid_json_arguments_fails(self):
        broken = ToolCall(name="create_reminder", arguments="{bad")
        self.assertFalse(grade(CALL_CASE, [broken]).passed)

    def test_wrong_tool_name_fails(self):
        self.assertFalse(
            grade(
                CALL_CASE,
                [tool_call(10, "关火", name="delete_all")],
            ).passed
        )

    def test_call_case_without_call_fails(self):
        self.assertFalse(grade(CALL_CASE, []).passed)

    # ---------- grade: no_call ----------

    def test_no_call_passes(self):
        self.assertTrue(grade(NO_CALL_CASE, []).passed)

    def test_no_call_fails_when_called(self):
        self.assertFalse(
            grade(NO_CALL_CASE, [tool_call(5)]).passed
        )

    # ---------- grade: at_most_one ----------

    def test_at_most_one_allows_zero_calls(self):
        self.assertTrue(grade(GUARD_CASE, []).passed)

    def test_at_most_one_rejects_two_calls(self):
        self.assertFalse(
            grade(GUARD_CASE, [tool_call(1), tool_call(2)]).passed
        )

    def test_at_most_one_rejects_non_positive_minutes(self):
        self.assertFalse(grade(GUARD_CASE, [tool_call(-5)]).passed)
        self.assertFalse(grade(GUARD_CASE, [tool_call(0)]).passed)

    # ---------- grade: no_mutation ----------

    def test_no_mutation_allows_read_only_tool(self):
        self.assertTrue(
            grade(NO_MUTATION_CASE, [list_call()]).passed
        )

    def test_no_mutation_allows_no_call(self):
        self.assertTrue(
            grade(NO_MUTATION_CASE, []).passed
        )

    def test_no_mutation_rejects_create(self):
        self.assertFalse(
            grade(
                NO_MUTATION_CASE,
                [tool_call(5)],
            ).passed
        )

    def test_no_mutation_rejects_cancel_and_update(self):
        for call in (cancel_call(), update_call()):
            self.assertFalse(
                grade(NO_MUTATION_CASE, [call]).passed
            )

    def test_no_mutation_rejects_mutation_after_list(self):
        # 先列后删同样算副作用。
        self.assertFalse(
            grade(
                NO_MUTATION_CASE,
                [list_call(), cancel_call()],
            ).passed
        )

    def test_parse_accepts_no_mutation(self):
        case = parse_case(
            {
                "id": "chat_04",
                "category": "other_intent",
                "input": "我现在有哪些提醒？",
                "expect": "no_mutation",
            }
        )

        self.assertEqual(case.expect, "no_mutation")

    # ---------- extract_tool_calls ----------

    def test_extract_from_message(self):
        message = SimpleNamespace(
            tool_calls=[
                SimpleNamespace(
                    function=SimpleNamespace(
                        name="create_reminder",
                        arguments='{"message": "关火", "minutes": 10}',
                    )
                )
            ]
        )

        calls = extract_tool_calls(message)

        self.assertEqual(len(calls), 1)
        self.assertTrue(grade(CALL_CASE, calls).passed)

        self.assertEqual(
            extract_tool_calls(SimpleNamespace(tool_calls=None)),
            [],
        )


if __name__ == "__main__":
    unittest.main()