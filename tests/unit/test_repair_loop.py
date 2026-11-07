"""修复闭环测试：先证明夹具真的会红，再证明闭环能把它们修绿。

离线：runner 用真实子进程（本机 Python，无网络），fixer 用脚本化替身。
"""

import unittest

from src.repair.fixtures import get_fixture, list_fixtures
from src.repair.loop import (
    RepairError,
    RepairLoop,
    RunResult,
    default_runner,
    parse_failures,
)


def ok_runner(files, *, modules, timeout):
    return RunResult(ok=True, failures=0, errors=0, output="OK", duration_ms=1.0)


def failing_runner(files, *, modules, timeout):
    return RunResult(
        ok=False, failures=1, errors=0, output="FAILED (failures=1)", duration_ms=1.0
    )


def solution_fixer(fixture, failure, files):
    """正确的修法：只返回夹具已知的 fixed_files。"""

    return dict(fixture.fixed_files)


def stubborn_fixer(fixture, failure, files):
    return dict(fixture.files)


def scripted_runner(results):
    queue = list(results)

    def runner(files, *, modules, timeout):
        return queue.pop(0)

    return runner


class TestFixtures(unittest.TestCase):
    def test_three_fixtures_are_registered(self):
        self.assertEqual(
            list_fixtures(), ["boundary", "missing_return", "off_by_one"]
        )

    def test_unknown_fixture_is_rejected(self):
        for bad in ("", "nope", None):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    get_fixture(bad)

    def test_each_fixture_really_fails(self):
        """夹具自检：有 bug 的版本必须真的跑红，否则闭环测的是空气。"""

        for name in list_fixtures():
            fixture = get_fixture(name)
            result = default_runner(
                fixture.files, modules=fixture.test_modules, timeout=60
            )

            with self.subTest(fixture=name):
                self.assertFalse(result.ok, f"{name} 的 bug 版居然通过了")
                self.assertGreater(result.problem_count, 0)

    def test_fixed_version_passes(self):
        for name in list_fixtures():
            fixture = get_fixture(name)
            result = default_runner(
                fixture.fixed_files, modules=fixture.test_modules, timeout=60
            )

            with self.subTest(fixture=name):
                self.assertTrue(result.ok, result.output[-400:])


class TestParseFailures(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(parse_failures("FAILED (failures=2, errors=1)"), (2, 1))
        self.assertEqual(parse_failures("OK"), (0, 0))
        self.assertEqual(parse_failures(""), (0, 0))
        self.assertEqual(parse_failures("FAILED (errors=3)"), (0, 3))


class TestLoop(unittest.TestCase):
    def test_already_green_skips_fix(self):
        calls = []

        loop = RepairLoop(
            get_fixture("off_by_one"),
            runner=ok_runner,
            fixer=lambda *args: calls.append(args) or {},
        )

        result = loop.run()

        self.assertTrue(result.success)
        self.assertEqual(result.rounds, 0)
        self.assertEqual(calls, [])
        self.assertEqual(result.score, 1.0)

    def test_single_repair_round(self):
        loop = RepairLoop(
            get_fixture("off_by_one"),
            runner=scripted_runner(
                [
                    RunResult(False, 1, 0, "IndexError", 1.0),
                    RunResult(True, 0, 0, "OK", 1.0),
                ]
            ),
            fixer=solution_fixer,
        )

        result = loop.run()

        self.assertTrue(result.success)
        self.assertEqual(result.rounds, 1)
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(result.attempts[1].changed_files, ("solution.py",))
        self.assertIn("IndexError", result.attempts[0].output_excerpt)

    def test_stubborn_fixer_hits_round_limit(self):
        loop = RepairLoop(
            get_fixture("boundary"),
            runner=failing_runner,
            fixer=stubborn_fixer,
            max_rounds=2,
        )

        result = loop.run()

        self.assertFalse(result.success)
        self.assertEqual(result.rounds, 2)
        self.assertIn("仍未通过", result.error)
        self.assertEqual(len(result.attempts), 3)
        self.assertLess(result.score, 1.0)

    def test_fixer_cannot_write_outside_fixture(self):
        loop = RepairLoop(
            get_fixture("off_by_one"),
            runner=failing_runner,
            fixer=lambda fixture, failure, files: {"../../evil.py": "x"},
        )

        result = loop.run()

        self.assertFalse(result.success)
        self.assertIn("夹具之外", result.error)

    def test_oversized_fix_is_rejected(self):
        loop = RepairLoop(
            get_fixture("off_by_one"),
            runner=failing_runner,
            fixer=lambda fixture, failure, files: {"solution.py": "x" * (16 * 1024 + 1)},
        )

        result = loop.run()

        self.assertFalse(result.success)
        self.assertIn("上限", result.error)

    def test_empty_fix_is_rejected(self):
        loop = RepairLoop(
            get_fixture("off_by_one"),
            runner=failing_runner,
            fixer=lambda fixture, failure, files: {},
        )

        result = loop.run()

        self.assertFalse(result.success)
        self.assertIn("没有改动", result.error)

    def test_timeout_counted_as_problem(self):
        loop = RepairLoop(
            get_fixture("off_by_one"),
            runner=scripted_runner(
                [
                    RunResult(False, 0, 1, "执行超时（60s）", 60000.0),
                    RunResult(True, 0, 0, "OK", 1.0),
                ]
            ),
            fixer=solution_fixer,
            max_rounds=1,
        )

        result = loop.run()

        self.assertTrue(result.success)
        self.assertEqual(result.attempts[0].errors, 1)

    def test_max_rounds_validation(self):
        for bad in (0, 6, True):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    RepairLoop(get_fixture("off_by_one"), max_rounds=bad)

    def test_result_serializes(self):
        loop = RepairLoop(
            get_fixture("off_by_one"), runner=ok_runner, fixer=solution_fixer
        )

        payload = loop.run().to_dict()

        self.assertEqual(payload["fixture"], "off_by_one")
        self.assertTrue(payload["success"])
        self.assertEqual(payload["attempts"][0]["round"], 0)


if __name__ == "__main__":
    unittest.main()
