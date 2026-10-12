"""Personal Agent Schema-driven Test Runner.

支持：
    audit        检查测试分类、Schema 与隔离审计，不运行测试
    unit         L1 单元测试
    integration  L2 数据集成测试
    api          L3 API 集成测试
    legacy       尚未分类的测试
    backend      全部后端测试

无需移动现有测试文件。

输出统一包含：
    Layer / Files / Cases / Duration / Result
"""

import argparse
import os
import sys
import time
import unittest

from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "tests"

TEST_LAYERS = (
    "unit",
    "integration",
    "api",
    "legacy",
)

COMMANDS = TEST_LAYERS + (
    "backend",
    "audit",
)


# ==========================================
# 初始化环境
# ==========================================

def prepare_environment():
    os.chdir(ROOT)

    # tests 目录放到模块搜索路径前面。
    # 这样可以导入 tests/registry.py，
    # 也可以加载根目录及子目录测试。
    for directory in (ROOT, TEST_ROOT):
        path = str(directory)

        if path in sys.path:
            sys.path.remove(path)

        sys.path.insert(0, path)


# ==========================================
# 发现测试文件
# ==========================================

def discover_files() -> list[Path]:

    if not TEST_ROOT.is_dir():
        raise RuntimeError(
            f"找不到 tests 目录：{TEST_ROOT}"
        )

    files = sorted(
        path.relative_to(TEST_ROOT)
        for path in TEST_ROOT.rglob("test_*.py")
        if path.is_file()
        and "__pycache__" not in path.parts
    )

    if not files:
        raise RuntimeError(
            "没有发现任何 Python 测试文件"
        )

    # 嵌套目录必须有 __init__.py。
    # 防止文件存在却不能被测试运行器导入。
    for relative in files:

        parent = relative.parent

        while parent != Path("."):

            init_path = (
                TEST_ROOT / parent / "__init__.py"
            )

            if not init_path.is_file():
                raise RuntimeError(
                    "测试目录缺少 __init__.py："
                    f"{init_path}"
                )

            parent = parent.parent

    return files


# ==========================================
# 读取与验证 Schema
# ==========================================

def load_registry():

    from registry import (
        TEST_SCHEMAS,
        TestSchema,
    )

    return TEST_SCHEMAS, TestSchema


def validate_registry(
    files: list[Path],
    schemas: dict,
    schema_type: type,
) -> None:

    known = {
        path.as_posix()
        for path in files
    }

    for filename, schema in schemas.items():

        if not isinstance(filename, str):
            raise RuntimeError(
                "Schema 注册表的路径必须是字符串"
            )

        if filename not in known:
            raise RuntimeError(
                "Schema 指向不存在的测试文件："
                f"{filename}"
            )

        if not isinstance(schema, schema_type):
            raise RuntimeError(
                "测试元数据类型无效："
                f"{filename}"
            )

        # 目录已经明确标注层级时，
        # 注册表不能标成另一个层级。
        relative = Path(filename)

        if len(relative.parts) > 1:

            directory_layer = relative.parts[0]

            if (
                directory_layer in (
                    "unit",
                    "integration",
                    "api",
                )
                and schema.layer != directory_layer
            ):
                raise RuntimeError(
                    "目录与 Schema 层级冲突："
                    f"{filename}"
                )


# ==========================================
# 分类
# ==========================================

def classify_file(
    relative: Path,
    schemas: dict,
) -> str:

    filename = relative.as_posix()

    # 注册表具有明确的分类信息。
    if filename in schemas:
        return schemas[filename].layer

    # 没有注册表记录时，
    # 保留已经建立的层级目录约定。
    if len(relative.parts) > 1:

        directory = relative.parts[0]

        if directory in (
            "unit",
            "integration",
            "api",
        ):
            return directory

    # 其余文件不会被丢弃。
    return "legacy"


def build_categories(
    files: list[Path],
    schemas: dict,
) -> dict:

    categories = {
        layer: []
        for layer in TEST_LAYERS
    }

    for relative in files:
        layer = classify_file(
            relative,
            schemas,
        )

        categories[layer].append(relative)

    return categories


# ==========================================
# 审计报告
# ==========================================

def print_report(
    files: list[Path],
    categories: dict,
    schemas: dict,
    duration: float = 0.0,
):

    print()
    print("Personal Agent Test Schema Audit")
    print("=" * 48)

    print(f"Layer: audit")
    print(f"Files: {len(files)}")
    print(f"Registered schemas: {len(schemas)}")

    print()

    for layer in TEST_LAYERS:
        count = len(categories[layer])

        print(
            f"{layer:<14} {count} files"
        )

    print()

    print("Registered test schemas:")

    for filename in sorted(schemas):

        schema = schemas[filename]

        print()
        print(f"  {filename}")
        print(f"    layer: {schema.layer}")
        print(f"    domain: {schema.domain}")
        print(f"    database: {schema.database}")
        print(
            "    authentication: "
            f"{schema.authentication}"
        )
        print(
            "    external_network: "
            f"{schema.external_network}"
        )
        print(f"    risk: {schema.risk}")

    print()
    print(
        "Note: metadata describes tests; "
        "it does not enforce network isolation."
    )
    print(f"Duration: {duration:.2f}s")
    print("=" * 48)


# ==========================================
# Python 模块名称
# ==========================================

def module_name(relative: Path) -> str:

    return ".".join(
        relative.with_suffix("").parts
    )


# ==========================================
# 执行测试
# ==========================================

def execute_tests(
    layer: str,
    files: list[Path],
    categories: dict,
    schemas: dict,
) -> int:

    if layer == "backend":
        selected = files
    else:
        selected = categories[layer]

    if not selected:
        print(
            f"ERROR: {layer} has no tests.",
            file=sys.stderr,
        )
        return 2

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    case_counts = defaultdict(int)
    empty_modules = []

    for relative in selected:

        name = module_name(relative)

        loaded = loader.loadTestsFromName(name)

        count = loaded.countTestCases()

        if count == 0:
            empty_modules.append(
                relative.as_posix()
            )

        category = classify_file(
            relative,
            schemas,
        )

        case_counts[category] += count

        suite.addTests(loaded)

    # 不能悄悄跳过没有被正确加载的测试文件。
    if empty_modules:

        for filename in empty_modules:
            print(
                f"ERROR: no tests in {filename}",
                file=sys.stderr,
            )

        return 2

    count = suite.countTestCases()

    print()
    print("Personal Agent Test Execution")
    print("=" * 48)
    print(f"Layer: {layer}")
    print(f"Files: {len(selected)}")
    print(f"Cases: {count}")

    if layer == "backend":

        print()
        print("Test case distribution:")

        for category in TEST_LAYERS:
            print(
                f"  {category:<14} "
                f"{case_counts[category]}"
            )

    print("=" * 48)
    print(flush=True)

    if count == 0:
        return 2

    started = time.monotonic()

    result = unittest.TextTestRunner(
        verbosity=1,
        failfast=False,
    ).run(suite)

    duration = time.monotonic() - started

    if (
        loader.errors
        or not result.wasSuccessful()
    ):

        print()
        print(f"FAIL: {layer}")
        print(
            f"Failures: {len(result.failures)}"
        )
        print(
            f"Errors: {len(result.errors)}"
        )
        print(f"Duration: {duration:.2f}s")
        print(f"Result: FAIL")

        return 1

    print()
    print(
        f"PASS: {layer}, "
        f"{result.testsRun} tests"
    )
    print(f"Duration: {duration:.2f}s")
    print(f"Result: PASS")

    return 0


# ==========================================
# 隔离审计
# ==========================================

def audit_isolation(
    files: list[Path],
    schemas: dict,
):
    """运行静态隔离审计，返回结果或 None。"""

    try:
        from audit_test_isolation import (
            print_audit,
            run_audit,
        )

    except ImportError:
        try:
            sys.path.insert(
                0,
                str(ROOT / "scripts"),
            )

            from audit_test_isolation import (
                print_audit,
                run_audit,
            )

        except ImportError as exc:
            print()
            print(
                "ERROR: 无法加载隔离审计模块："
                f"{exc}",
                file=sys.stderr,
            )

            return None

    started = time.monotonic()

    outcome = run_audit(files, schemas)

    print_audit(outcome)

    outcome["duration"] = time.monotonic() - started
    outcome["ok"] = not outcome["violations"]

    return outcome


# ==========================================
# 主入口
# ==========================================

def run(layer: str) -> int:

    prepare_environment()

    schemas, schema_type = load_registry()

    files = discover_files()

    validate_registry(
        files,
        schemas,
        schema_type,
    )

    categories = build_categories(
        files,
        schemas,
    )

    if layer == "audit":

        started = time.monotonic()

        print_report(
            files,
            categories,
            schemas,
            duration=time.monotonic() - started,
        )

        outcome = audit_isolation(files, schemas)

        if outcome is None:
            print()
            print("FAIL: Test Schema audit")
            print("Result: FAIL")

            return 2

        print()
        print(f"Duration: {outcome['duration']:.2f}s")
        print(f"Result: {'PASS' if outcome['ok'] else 'FAIL'}")
        print()
        print("PASS: Test Schema audit")

        return 0

    return execute_tests(
        layer,
        files,
        categories,
        schemas,
    )


def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "Personal Agent test runner"
        )
    )

    parser.add_argument(
        "layer",
        choices=COMMANDS,
    )

    args = parser.parse_args()

    try:
        return run(args.layer)

    except Exception as exc:

        print(
            f"Test runner error: {exc}",
            file=sys.stderr,
        )

        return 2


if __name__ == "__main__":
    raise SystemExit(main())