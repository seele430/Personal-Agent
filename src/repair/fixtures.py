"""固定 bug 夹具：三个小到能一眼看懂、但真的会红的缺陷。

为什么要"固定"：修复闭环最怕的是"随便找个 bug 演示一下"——那种演示
不可复现。这里每个夹具都带 **有 bug 的源码 + 会失败的测试 + 已知的正确
修法**，所以"修好了"可以被机器判定，不靠人看。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BugFixture:
    name: str
    description: str
    files: dict[str, str]
    fixed_files: dict[str, str]
    test_modules: tuple[str, ...] = ("test_solution",)
    #: 该夹具的测试总数（用于评分）
    test_count: int = 3

    def all_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.files))


_OFF_BY_ONE = """\
def last_item(items):
    \"\"\"返回最后一个元素。\"\"\"
    return items[len(items)]


def first_item(items):
    return items[0]
"""

_OFF_BY_ONE_FIXED = """\
def last_item(items):
    \"\"\"返回最后一个元素。\"\"\"
    return items[-1]


def first_item(items):
    return items[0]
"""

_OFF_BY_ONE_TEST = """\
import unittest

from solution import first_item, last_item


class LastItemTest(unittest.TestCase):
    def test_last_of_three(self):
        self.assertEqual(last_item([1, 2, 3]), 3)

    def test_last_of_single(self):
        self.assertEqual(last_item([9]), 9)

    def test_first(self):
        self.assertEqual(first_item([1, 2, 3]), 1)
"""

_BOUNDARY = """\
def is_adult(age):
    \"\"\"成年判定：18 岁（含）以上算成年。\"\"\"
    return age > 18
"""

_BOUNDARY_FIXED = """\
def is_adult(age):
    \"\"\"成年判定：18 岁（含）以上算成年。\"\"\"
    return age >= 18
"""

_BOUNDARY_TEST = """\
import unittest

from solution import is_adult


class AdultTest(unittest.TestCase):
    def test_boundary_eighteen(self):
        self.assertTrue(is_adult(18))

    def test_underage(self):
        self.assertFalse(is_adult(17))

    def test_adult(self):
        self.assertTrue(is_adult(19))
"""

_MISSING_RETURN = """\
def total(values):
    \"\"\"返回所有值的和。\"\"\"
    running = 0
    for value in values:
        running += value
"""

_MISSING_RETURN_FIXED = """\
def total(values):
    \"\"\"返回所有值的和。\"\"\"
    running = 0
    for value in values:
        running += value
    return running
"""

_MISSING_RETURN_TEST = """\
import unittest

from solution import total


class TotalTest(unittest.TestCase):
    def test_sum_three(self):
        self.assertEqual(total([1, 2, 3]), 6)

    def test_empty(self):
        self.assertEqual(total([]), 0)

    def test_single(self):
        self.assertEqual(total([5]), 5)
"""


FIXTURES: dict[str, BugFixture] = {
    "off_by_one": BugFixture(
        name="off_by_one",
        description="last_item 用 items[len(items)]，越界 IndexError",
        files={"solution.py": _OFF_BY_ONE, "test_solution.py": _OFF_BY_ONE_TEST},
        fixed_files={
            "solution.py": _OFF_BY_ONE_FIXED,
            "test_solution.py": _OFF_BY_ONE_TEST,
        },
    ),
    "boundary": BugFixture(
        name="boundary",
        description="is_adult 用 > 18，边界 18 岁被判成未成年",
        files={"solution.py": _BOUNDARY, "test_solution.py": _BOUNDARY_TEST},
        fixed_files={
            "solution.py": _BOUNDARY_FIXED,
            "test_solution.py": _BOUNDARY_TEST,
        },
    ),
    "missing_return": BugFixture(
        name="missing_return",
        description="total 累加后忘了 return，返回 None",
        files={
            "solution.py": _MISSING_RETURN,
            "test_solution.py": _MISSING_RETURN_TEST,
        },
        fixed_files={
            "solution.py": _MISSING_RETURN_FIXED,
            "test_solution.py": _MISSING_RETURN_TEST,
        },
    ),
}


def get_fixture(name: str) -> BugFixture:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("夹具名不能为空")

    try:
        return FIXTURES[name.strip()]

    except KeyError:
        raise ValueError(
            f"未知夹具：{name}，可选 {sorted(FIXTURES)}"
        ) from None


def list_fixtures() -> list[str]:
    return sorted(FIXTURES)
