"""Personal Agent Schema-driven Test Runner.

支持：
    audit        检查测试分类、Schema、层归属与隔离审计，不运行测试
    unit         L1 单元测试
    integration  L2 数据集成测试
    api          L3 API 集成测试
    legacy       尚未分类的测试
    backend      全部后端测试
    list [layer] 列出层归属（来自注册表的显式契约），--doc 生成/校验索引

无需移动现有测试文件；层归属由 tests/registry.py 决定，与目录无关。

输出统一包含：
    Layer / Files / Cases / Duration / Result
"""

import argparse
import os
import re
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
    "list",
)

LAYER_TITLES = {
    "unit": "L1 单元测试",
    "integration": "L2 数据集成测试",
    "api": "L3 HTTP/API 测试",
    "legacy": "未分类测试",
}

LAYERS_DOC = TEST_ROOT / "TEST_LAYERS.md"
DIGEST_PREFIX = "membership-digest:"


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
        LAYER_MEMBERSHIP,
        TEST_SCHEMAS,
        TestSchema,
        layer_of,
        membership_digest,
        validate_membership,
    )

    return {
        "schemas": TEST_SCHEMAS,
        "schema_type": TestSchema,
        "layer_of": layer_of,
        "membership": LAYER_MEMBERSHIP,
        "digest": membership_digest,
        "validate": validate_membership,
    }


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
    # 层归属是 tests/registry.py 里的显式契约，
    # 与文件所在目录无关。
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
# 层归属查询与索引文档
# ==========================================

def render_layers_doc(registry: dict, files: list) -> str:
    """Render tests/TEST_LAYERS.md from the registry."""

    schemas = registry["schemas"]

    known = {path.as_posix() for path in files}

    lines = [
        "# 测试分层索引（TEST_LAYERS.md）",
        "",
        "> 本文件由 `python scripts/run_tests.py list --doc` 生成，请勿手工编辑。",
        "> 校验：`python scripts/run_tests.py audit` 会比对摘要，文档过期将报 FAIL。",
        "",
        "## 层与物理位置的关系",
        "",
        "- **层归属由 `tests/registry.py` 的 Schema 决定，与文件所在目录无关。**",
        "- 因此 `tests/` 根目录下平铺的测试同样属于某一层；迁移文件位置不改变层归属。",
        "- 目录约定只作为未登记文件的兜底：`unit/`、`integration/`、`api/`，其余归 `legacy`。",
        "",
        "## 检索命令",
        "",
        "```",
        "python scripts/run_tests.py list              # 列出全部层",
        "python scripts/run_tests.py list integration  # 只列某一层",
        "python scripts/run_tests.py integration       # 运行该层",
        "```",
        "",
        f"<!-- membership-digest: {registry['digest']()} -->",
        "",
    ]

    for layer in ("unit", "integration", "api", "legacy"):
        members = (
            sorted(registry["membership"][layer])
            if layer in registry["membership"]
            else []
        )

        title = LAYER_TITLES.get(layer, layer)

        lines.append(f"## {layer} — {title} ({len(members)} files)")
        lines.append("")

        if not members:
            lines.append("（无）")
            lines.append("")
            continue

        lines.append("| File | 物理位置 | Domain | DB | Network | Risk | Status |")
        lines.append("|---|---|---|---|---|---|---|")

        for filename in members:
            schema = schemas.get(filename)

            physical = (
                "tests/ 根"
                if "/" not in filename
                else f"tests/{filename.split('/')[0]}/"
            )

            if schema is None:
                lines.append(
                    f"| `{filename}` | {physical} | - | - | - | - | "
                    f"未登记 |"
                )
                continue

            status = (
                "registered"
                if filename in ORIGINAL_SCHEMA_NAMES
                else "registered-inferred"
            )

            present = "" if filename in known else " ⚠缺失"

            lines.append(
                f"| `{filename}`{present} | {physical} | {schema.domain} | "
                f"{schema.database} | {schema.external_network} | "
                f"{schema.risk} | {status} |"
            )

        lines.append("")

    return "\n".join(lines)


# 由 run_tests 使用的原始登记集合（与 registry.ORIGINAL_SCHEMAS 保持同步的只读副本）
ORIGINAL_SCHEMA_NAMES = frozenset({
    "unit/test_agent_tool_limits.py",
    "integration/test_reminder_store.py",
    "test_ai_budget.py",
    "test_ai_budget_reconcile.py",
    "test_reminder_api.py",
    "test_reminder_chat_http.py",
})


def check_doc(registry: dict) -> str | None:
    """Return a problem string when TEST_LAYERS.md is out of date."""

    if not LAYERS_DOC.is_file():
        return (
            "tests/TEST_LAYERS.md 不存在，请运行 "
            "`python scripts/run_tests.py list --doc`"
        )

    text = LAYERS_DOC.read_text(encoding="utf-8", errors="replace")

    marker = re.search(
        rf"{DIGEST_PREFIX}\s*([0-9a-f]+)",
        text,
    )

    if not marker:
        return "tests/TEST_LAYERS.md 缺少 membership-digest 标记"

    expected = registry["digest"]()

    if marker.group(1) != expected:
        return (
            "tests/TEST_LAYERS.md 已过期"
            f"（文档 {marker.group(1)} != 代码 {expected}），"
            "请运行 `python scripts/run_tests.py list --doc`"
        )

    return None


def list_layers(
    registry: dict,
    files: list,
    write_doc: bool,
    target: str = None,
) -> int:
    """Print layer membership; optionally write the index document."""

    if write_doc:
        LAYERS_DOC.write_text(
            render_layers_doc(registry, files) + "\n",
            encoding="utf-8",
        )

        print()
        print(f"Wrote: {LAYERS_DOC}")

    print()
    print("Personal Agent Test Layers")
    print("=" * 48)

    selected = (
        (target,)
        if target
        else ("unit", "integration", "api", "legacy")
    )

    total = 0

    for layer in selected:
        members = (
            sorted(registry["membership"][layer])
            if layer in registry["membership"]
            else []
        )

        total += len(members)

        print()
        print(
            f"{layer} — {LAYER_TITLES.get(layer, layer)} "
            f"({len(members)} files)"
        )

        for filename in members:
            physical = (
                "tests/ 根"
                if "/" not in filename
                else f"tests/{filename.split('/')[0]}/"
            )

            print(f"  {filename:<48} [{physical}]")

    print()
    print(f"Total registered: {total}")
    print(
        "Note: layer comes from tests/registry.py, "
        "not from the directory layout."
    )

    if not write_doc:
        problem = check_doc(registry)

        if problem:
            print()
            print(f"Document check: {problem}")

    return 0


# ==========================================
# 主入口
# ==========================================

def run(layer: str, write_doc: bool = False, target: str = None) -> int:

    prepare_environment()

    registry = load_registry()

    schemas = registry["schemas"]

    files = discover_files()

    try:
        validate_registry(
            files,
            schemas,
            registry["schema_type"],
        )

    except RuntimeError as exc:

        # 注册表过期会直接打断运行。这里先打印层归属体检，
        # 让告警（例如新测试未登记、将落入 legacy）也能被看到。
        if layer == "audit":
            membership = registry["validate"](files)

            print()
            print("Layer membership")
            print("-" * 48)

            print(f"  VIOLATION [registry] {exc}")

            for problem in membership["violations"]:
                if "已登记但磁盘上不存在" in problem:
                    continue

                print()
                print(f"  VIOLATION [membership] {problem}")

            for problem in membership["warnings"]:
                print()
                print(f"  warning [membership] {problem}")

            print()
            print("Result: FAIL")

        raise

    if layer == "list":
        return list_layers(registry, files, write_doc, target)

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

        membership = registry["validate"](files)

        print()
        print("Layer membership")
        print("-" * 48)

        for name in ("unit", "integration", "api"):
            print(
                f"  {name:<14} "
                f"{len(registry['membership'][name])} files"
            )

        print(
            "  source: schema layer in tests/registry.py "
            "(not the directory layout)"
        )

        for problem in membership["violations"]:
            print()
            print(f"  VIOLATION [membership] {problem}")

        for problem in membership["warnings"]:
            print()
            print(f"  warning [membership] {problem}")

        doc_problem = check_doc(registry)

        if doc_problem:
            print()
            print(f"  VIOLATION [document] {doc_problem}")

        outcome = audit_isolation(files, schemas)

        if outcome is None:
            print()
            print("FAIL: Test Schema audit")
            print("Result: FAIL")

            return 2

        ok = (
            outcome["ok"]
            and not membership["violations"]
            and doc_problem is None
        )

        print()
        print(f"Duration: {outcome['duration']:.2f}s")
        print(f"Result: {'PASS' if ok else 'FAIL'}")

        if not ok:
            print()
            print("FAIL: Test Schema audit")

            return 2

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
        nargs="?",
        default="backend",
        choices=COMMANDS,
    )

    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        choices=("unit", "integration", "api", "legacy"),
        help="与 list 同用：只显示指定层",
    )

    parser.add_argument(
        "--doc",
        action="store_true",
        help="与 list 同用：写入或校验 tests/TEST_LAYERS.md",
    )

    args = parser.parse_args()

    try:
        return run(args.layer, args.doc, args.target)

    except Exception as exc:

        print(
            f"Test runner error: {exc}",
            file=sys.stderr,
        )

        return 2


if __name__ == "__main__":
    raise SystemExit(main())