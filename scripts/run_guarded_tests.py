"""Personal Agent protected test runner.

Pilot mode:
- Reuse the existing Schema-based test runner.
- Enable the Python runtime safety guard before loading tests.
- Replace model credentials with a deliberately invalid key.
- Do not change the existing test.ps1 or run_tests.py.

This is not an operating-system sandbox.
"""

import argparse
import os
import sys

from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from scripts.run_tests import run
from scripts.test_safety import guarded_test_runtime


LAYERS = (
    "unit",
    "integration",
    "api",
    "legacy",
    "backend",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Personal Agent protected test runner"
        )
    )

    parser.add_argument(
        "layer",
        choices=LAYERS,
    )

    args = parser.parse_args()

    # These values are set before importing test modules.
    # Their original values are restored after execution.
    safe_environment = {
        "PERSONAL_AGENT_TEST_MODE": "1",
        "OPENAI_API_KEY": (
            "personal-agent-invalid-test-key"
        ),
        "NTFY_SERVER": "http://127.0.0.1:1",
    }

    print()
    print("=" * 48)
    print("Personal Agent Protected Test Runner")
    print("=" * 48)
    print(f"Layer: {args.layer}")
    print("Python SQLite guard: enabled")
    print("Python socket guard: enabled")
    print("Real model API key: replaced")
    print()
    print(
        "Note: this is a Python runtime guard, "
        "not a complete system sandbox."
    )
    print("=" * 48)
    print(flush=True)

    try:
        # Restore environment variables automatically.
        with patch.dict(
            os.environ,
            safe_environment,
        ):

            # Activate protection before the existing
            # runner imports and executes test modules.
            with guarded_test_runtime(ROOT):

                result = run(args.layer)

        if result == 0:
            print()
            print(
                f"PROTECTED TEST PASS: {args.layer}"
            )
        else:
            print()
            print(
                f"PROTECTED TEST FAILED: "
                f"{args.layer}"
            )

        return result

    except Exception as exc:
        print(
            f"Protected runner error: {exc}",
            file=sys.stderr,
        )

        return 2


if __name__ == "__main__":
    raise SystemExit(main())