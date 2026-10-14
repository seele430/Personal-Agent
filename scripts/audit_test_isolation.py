"""Personal Agent test isolation audit (static, read-only).

This module never imports or executes a test module.
It reads test sources and reports isolation violations.

Checks:

1. Database
   A test must not open the real project databases
   (chat_requests.db, tasks.db, auth_users.db, reminders.db,
   ai_usage.db).
   A database file created under a temporary root is fine.
   A project database path used only to prove that the safety
   guard blocks it is reported as a warning, not a violation.

2. Network
   requests / httpx / urllib calls and raw TCP/UDP socket calls
   are violations unless they sit inside a mock or a guard.

3. Schema consistency
   A registered schema must not declare database="real" or
   external_network="real".

4. Layer directory registration
   A test inside tests/unit, tests/integration or tests/api
   should have a registered schema. Missing ones are warnings.

Lines are checked after removing trailing # comments, so code
lines that carry Chinese comments or Chinese string literals are
audited too.

Schema metadata describes tests. This audit is a static source
review, not an operating-system sandbox.
"""

import re

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = PROJECT_ROOT / "tests"

LAYER_DIRECTORIES = (
    "unit",
    "integration",
    "api",
)

REAL_DATABASE_FILES = (
    "chat_requests.db",
    "tasks.db",
    "auth_users.db",
    "reminders.db",
    "ai_usage.db",
)

TEMP_MARKERS = (
    "TemporaryDirectory",
    "mkdtemp",
    ":memory:",
    "temp_dir",
    "tempdir",
    "tmp_path",
    "self.temp",
    "temp.name",
)

# real HTTP client calls
HTTP_CALL = re.compile(
    r"\b(requests|httpx)\s*\.\s*"
    r"(get|post|put|patch|delete|head|options|request|stream)\s*\("
)

# raw socket calls, excluding sqlite3 helper methods
SOCKET_CALL = re.compile(r"\.\s*(connect|connect_ex|sendto|sendall)\s*\(")

URLLIB_CALL = re.compile(r"\b(urlopen|urlretrieve)\s*\(")

GUARD_WORDS = re.compile(
    r"patch|mock|Mock|assertRaises|guarded_test_runtime|"
    r"TestSafetyViolation"
)

TEMP_ASSIGN = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_\.]*)\s*=\s*.*"
    r"(TemporaryDirectory|mkdtemp|self\.temp)"
)

TEMP_FROM_TEMP = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_\.]*)\s*=\s*.*"
    r"(self\.temp|temp_dir|tempdir|temp\.name)"
)

NOT_NETWORK = re.compile(r"sqlite3|closing|self\.connect")

DOCSTRING = '"""'


def relative_files():
    return sorted(
        path
        for path in TEST_ROOT.rglob("test_*.py")
        if "__pycache__" not in path.parts
    )


def read_lines(path: Path):
    return path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines()


def temp_roots(lines) -> set:
    """Variable names that hold a temporary directory."""

    names = set()

    for line in lines:
        match = TEMP_ASSIGN.match(line)

        if match:
            names.add(match.group(1))

        match = TEMP_FROM_TEMP.match(line)

        if match:
            names.add(match.group(1))

    return names


def strip_inline_comment(line: str) -> str:
    """Remove a trailing # comment that is not inside a string literal."""

    quote = None
    escaped = False

    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue

        if quote:
            if char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue

        if char in ("'", '"'):
            quote = char
        elif char == "#":
            return line[:index]

    return line


def code_lines(lines):
    """Yield (number, code) outside comments and docstrings.

    Trailing # comments are removed; the remaining code is always
    audited, including code that contains Chinese text.
    """

    in_docstring = False

    for number, line in enumerate(lines, start=1):
        if in_docstring:
            if DOCSTRING in line:
                in_docstring = False

            continue

        stripped = line.strip()

        if stripped.count(DOCSTRING) == 1:
            in_docstring = True

            if stripped.startswith(DOCSTRING):
                continue

        if stripped.startswith("#"):
            continue

        code = strip_inline_comment(line)

        if not code.strip():
            continue

        yield number, code


def audit_database(lines) -> list:
    findings = []
    roots = temp_roots(lines)

    for number, line in code_lines(lines):
        stripped = line.strip()

        for name in REAL_DATABASE_FILES:
            if name not in line:
                continue

            if f"test_{name}" in line:
                # temporary name such as test_tasks.db
                findings.append(
                    {
                        "kind": "database/warning",
                        "line": number,
                        "text": stripped[:140],
                        "reason": (
                            f"{name} appears only as the base name of "
                            f"a temporary file"
                        ),
                    }
                )
                continue

            under_temp = any(marker in line for marker in TEMP_MARKERS)

            if not under_temp:
                for root in roots:
                    if root in line:
                        under_temp = True
                        break

            if under_temp:
                findings.append(
                    {
                        "kind": "database/warning",
                        "line": number,
                        "text": stripped[:140],
                        "reason": (
                            f"{name} is resolved under a temporary root"
                        ),
                    }
                )
                continue

            findings.append(
                {
                    "kind": "database",
                    "line": number,
                    "text": stripped[:140],
                    "reason": (
                        f"test source points at the real project "
                        f"database {name}"
                    ),
                }
            )

    return findings


def audit_network(lines) -> list:
    findings = []
    guarded_block = False

    for number, line in code_lines(lines):
        stripped = line.strip()

        if "with guarded_test_runtime" in line:
            guarded_block = True
            continue

        if guarded_block and re.match(r"^\s*(def |class )", line):
            guarded_block = False

        if GUARD_WORDS.search(line):
            continue

        if guarded_block:
            continue

        if NOT_NETWORK.search(line):
            continue

        if HTTP_CALL.search(line):
            findings.append(
                {
                    "kind": "network",
                    "line": number,
                    "text": stripped[:140],
                    "reason": (
                        "HTTP client call outside a mock or guard"
                    ),
                }
            )
            continue

        if SOCKET_CALL.search(line):
            findings.append(
                {
                    "kind": "network",
                    "line": number,
                    "text": stripped[:140],
                    "reason": (
                        "raw socket call outside a mock or guard"
                    ),
                }
            )
            continue

        if URLLIB_CALL.search(line):
            findings.append(
                {
                    "kind": "network",
                    "line": number,
                    "text": stripped[:140],
                    "reason": "urlopen/urlretrieve outside a guard",
                }
            )

    return findings


def audit_imports(lines) -> list:
    findings = []
    text = "\n".join(lines)

    match = re.search(
        r"^\s*(import|from)\s+(requests|httpx|urllib\S*|socket)\b",
        text,
        re.M,
    )

    if not match:
        return findings

    # A temporary directory says nothing about network isolation,
    # so only mocks, guards and TestClient count here.
    guarded = bool(GUARD_WORDS.search(text)) or bool(
        re.search(r"TestClient", text)
    )

    if not guarded:
        findings.append(
            {
                "kind": "network",
                "line": 0,
                "text": match.group(0).strip(),
                "reason": (
                    "module imports a network library and shows no "
                    "mock, guard or TestClient marker"
                ),
            }
        )

    return findings


def audit_schema(schemas: dict) -> list:
    findings = []

    for filename, schema in sorted(schemas.items()):
        if schema.database == "real":
            findings.append(
                {
                    "kind": "schema",
                    "file": filename,
                    "line": 0,
                    "text": "database=real",
                    "reason": (
                        "schema declares a real database; tests must "
                        "use temporary or in-memory SQLite"
                    ),
                }
            )

        if schema.external_network == "real":
            findings.append(
                {
                    "kind": "schema",
                    "file": filename,
                    "line": 0,
                    "text": "external_network=real",
                    "reason": "schema declares a real external network",
                }
            )

    return findings


def dedupe(findings: list) -> list:
    """Keep one finding per (kind, file, line)."""

    seen = set()
    result = []

    for finding in findings:
        key = (
            finding["kind"],
            finding.get("file"),
            finding.get("line"),
        )

        if key in seen:
            continue

        seen.add(key)
        result.append(finding)

    return result


def run_audit(files: list, schemas: dict) -> dict:
    violations = []
    warnings = []

    for relative_path in files:
        path = TEST_ROOT / relative_path
        lines = read_lines(path)
        relative = relative_path.as_posix()

        for finding in (
            audit_database(lines)
            + audit_network(lines)
            + audit_imports(lines)
        ):
            finding["file"] = relative

            if finding["kind"].endswith("warning"):
                warnings.append(finding)
            else:
                violations.append(finding)

        parts = relative_path.parts

        if len(parts) > 1 and parts[0] in LAYER_DIRECTORIES:
            if relative not in schemas:
                warnings.append(
                    {
                        "kind": "registry/warning",
                        "file": relative,
                        "line": 0,
                        "text": f"layer directory {parts[0]}",
                        "reason": (
                            "test lives in a layer directory but has no "
                            "registered schema"
                        ),
                    }
                )

    violations.extend(audit_schema(schemas))

    return {
        "files": len(files),
        "violations": dedupe(violations),
        "warnings": dedupe(warnings),
    }


def print_audit(result: dict) -> None:
    print()
    print("Isolation audit (static source review)")
    print("-" * 48)
    print(f"Files reviewed: {result['files']}")
    print(f"Violations: {len(result['violations'])}")
    print(f"Warnings: {len(result['warnings'])}")

    for finding in result["violations"]:
        location = finding.get("file", "?")

        if finding.get("line"):
            location = f"{location}:{finding['line']}"

        print()
        print(f"  VIOLATION [{finding['kind']}] {location}")
        print(f"    {finding['text']}")
        print(f"    reason: {finding['reason']}")

    for finding in result["warnings"]:
        location = finding.get("file", "?")

        if finding.get("line"):
            location = f"{location}:{finding['line']}"

        print()
        print(f"  warning [{finding['kind']}] {location}")
        print(f"    {finding['text']}")
        print(f"    reason: {finding['reason']}")

    print()
    print(
        "Note: database and network facts come from static source "
        "review, not from a runtime sandbox."
    )


if __name__ == "__main__":
    import sys

    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    from scripts.run_tests import discover_files
    from tests.registry import TEST_SCHEMAS

    outcome = run_audit(discover_files(), TEST_SCHEMAS)
    print_audit(outcome)

    raise SystemExit(1 if outcome["violations"] else 0)