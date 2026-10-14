"""Personal Agent protected test runner.

- Reuses the existing Schema-based test runner (scripts/run_tests.py).
- Enables the Python runtime safety guard (SQLite + socket) before
  loading tests.
- Points PERSONAL_AGENT_DATA_DIR at a fresh temporary directory, so
  every database (tasks / reminders / chat / auth / usage) lands
  outside the project even when a test forgets to patch its paths.
  The SQLite guard stays as the second line of defence.
- Replaces model credentials, provider settings and ntfy settings with
  fixed fake values, so real values from .env never reach the test
  process (load_dotenv does not override variables that already exist).
- scripts/test.ps1 calls this runner for every backend layer.

This is not an operating-system sandbox.
"""

import argparse
import os
import shutil
import sys
import tempfile

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


# Keep this mapping in sync with $TestEnvironment in scripts/test.ps1.
SAFE_ENVIRONMENT = {
    "PERSONAL_AGENT_TEST_MODE": "1",
    "OPENAI_API_KEY": "personal-agent-invalid-test-key",
    # Fixed to a priced host/model so results do not depend on the
    # local .env. Real network access is still blocked by the socket guard.
    "OPENAI_BASE_URL": "https://api.deepseek.com",
    "MODEL": "deepseek-flash",
    "NTFY_SERVER": "http://127.0.0.1:1",
    # An ntfy topic works like an access credential; never reuse the real one.
    "NTFY_TOPIC": "personal-agent-test-topic",
}


# Must match DATA_DIR_ENV in src/config.py.
DATA_DIR_ENV = "PERSONAL_AGENT_DATA_DIR"

# Temporary directory that holds every database of this test run.
DATA_DIR_PREFIX = "personal-agent-test-data-"


def build_environment(data_dir: Path) -> dict:
    """Safe environment plus the isolated data directory."""

    environment = dict(SAFE_ENVIRONMENT)
    environment[DATA_DIR_ENV] = str(data_dir)

    return environment


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Personal Agent protected test runner"
    )

    parser.add_argument(
        "layer",
        choices=LAYERS,
    )

    args = parser.parse_args()

    # The directory must exist before src.config is imported, so every
    # module resolves its database paths inside the temporary tree.
    data_dir = Path(
        tempfile.mkdtemp(prefix=DATA_DIR_PREFIX)
    ).resolve()

    print()
    print("=" * 48)
    print("Personal Agent Protected Test Runner")
    print("=" * 48)
    print(f"Layer: {args.layer}")
    print("Python SQLite guard: enabled")
    print("Python socket guard: enabled")
    print("Real model API key: replaced")
    print("Provider / model / ntfy settings: replaced with fake values")
    print(f"Data directory: {data_dir}")
    print()
    print(
        "Note: this is a Python runtime guard, "
        "not a complete system sandbox."
    )
    print("=" * 48)
    print(flush=True)

    try:
        # patch.dict restores the original environment on exit.
        with patch.dict(os.environ, build_environment(data_dir)):

            # Activate protection before the existing runner
            # imports and executes test modules.
            with guarded_test_runtime(ROOT):
                result = run(args.layer)

        print()

        if result == 0:
            print(f"PROTECTED TEST PASS: {args.layer}")
        else:
            print(f"PROTECTED TEST FAILED: {args.layer}")

        return result

    except Exception as exc:
        print(
            f"Protected runner error: {exc}",
            file=sys.stderr,
        )

        return 2

    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
