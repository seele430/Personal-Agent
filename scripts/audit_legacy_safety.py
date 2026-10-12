"""Personal Agent Legacy test safety audit.

Static analysis only:
- Does not import or execute test modules.
- Does not open SQLite databases.
- Does not contact external services.
- Does not modify project files.

Findings indicate items requiring manual review.
They do not prove a test is unsafe or safe.
"""

import ast
import sys

from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "tests"

sys.path.insert(0, str(TEST_ROOT))

from registry import TEST_SCHEMAS


LAYERS = {"unit", "integration", "api"}

PRIORITY = {
    "network": 4,
    "process": 4,
    "database": 3,
    "file_write": 2,
}


def call_name(node):
    if isinstance(node, ast.Name):
        return node.id

    if isinstance(node, ast.Attribute):
        parent = call_name(node.value)

        if parent:
            return parent + "." + node.attr

        return node.attr

    return ""


def classify_call(name):
    leaf = name.rsplit(".", 1)[-1]

    if (
        name.startswith("sqlite3.")
        or leaf in {
            "connect_db",
            "init_db",
            "init_chat_db",
            "create_reminder",
            "migrate_chat_ownership",
        }
    ):
        return "database"

    if (
        name.startswith((
            "requests.",
            "httpx.",
            "urllib.request.",
            "socket.",
            "aiohttp.",
        ))
        or leaf in {
            "send_notification",
            "urlopen",
            "OpenAI",
        }
    ):
        return "network"

    if (
        name.startswith("subprocess.")
        or name == "os.system"
        or leaf in {"Popen"}
    ):
        return "process"

    if leaf in {
        "write_text",
        "write_bytes",
        "unlink",
        "rmtree",
        "remove",
        "rename",
        "replace",
    }:
        return "file_write"

    return None


def is_legacy(relative):
    if relative.as_posix() in TEST_SCHEMAS:
        return False

    if (
        len(relative.parts) > 1
        and relative.parts[0] in LAYERS
    ):
        return False

    return True


def scan_file(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    findings = {}

    def record(category, line):
        findings.setdefault(category, set()).add(line)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = call_name(node.func)

            category = classify_call(name)

            if category:
                record(category, node.lineno)

        # Detect references to commonly used database paths.
        if isinstance(node, ast.Name):
            if (
                node.id == "DB_PATH"
                or node.id.endswith("_DB_PATH")
            ):
                record("database", node.lineno)

        if isinstance(node, ast.Attribute):
            if (
                node.attr == "DB_PATH"
                or node.attr.endswith("_DB_PATH")
            ):
                record("database", node.lineno)

        # Database filenames may reveal a dependency
        # that does not use sqlite3.connect directly.
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
        ):
            value = node.value.lower()

            if (
                len(value) <= 260
                and ".db" in value
            ):
                record("database", node.lineno)

    return findings


def main():
    if not TEST_ROOT.is_dir():
        print("ERROR: tests directory not found")
        return 2

    files = sorted(
        path
        for path in TEST_ROOT.rglob("test_*.py")
        if (
            path.is_file()
            and "__pycache__" not in path.parts
            and is_legacy(path.relative_to(TEST_ROOT))
        )
    )

    if not files:
        print("ERROR: no Legacy test files found")
        return 2

    results = []
    totals = Counter()

    for path in files:
        try:
            findings = scan_file(path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            print(f"ERROR: cannot analyze {path}: {exc}")
            return 2

        relative = path.relative_to(TEST_ROOT).as_posix()

        for category in findings:
            totals[category] += 1

        if findings:
            score = sum(
                PRIORITY.get(category, 1)
                for category in findings
            )
            results.append((score, relative, findings))

    results.sort(key=lambda item: (-item[0], item[1]))

    print()
    print("Personal Agent - Legacy Safety Audit")
    print("=" * 55)
    print(f"Legacy files checked: {len(files)}")
    print(f"Files requiring review: {len(results)}")
    print()

    print("Potential dependency categories:")

    for category in (
        "database",
        "network",
        "process",
        "file_write",
    ):
        print(
            f"  {category:<12} "
            f"{totals[category]} files"
        )

    print()
    print("Review candidates (highest priority first):")
    print("-" * 55)

    # Limit output so the terminal stays readable.
    for _, filename, findings in results[:20]:
        print(f"\n{filename}")

        for category, lines in sorted(findings.items()):
            shown = ",".join(
                str(line)
                for line in sorted(lines)[:5]
            )

            print(f"  {category}: lines {shown}")

    if len(results) > 20:
        print()
        print(
            f"... {len(results) - 20} more files "
            "also require review."
        )

    print()
    print("=" * 55)
    print("Static audit completed.")
    print(
        "WARNING: no findings does not mean "
        "the test is isolated."
    )
    print(
        "No Legacy tests were executed."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())