"""Measure the existing API's cold-start readiness without changing app code."""

from __future__ import annotations

import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / ".runtime" / "d2-startup"
OUTPUT = ROOT / "docs" / "evidence" / "d2-startup.json"
RUNS = 5
READY_TIMEOUT_SECONDS = 45


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def stop_process(process: subprocess.Popen[str]) -> tuple[str, list[str]]:
    """Stop only the child started by this script and return output and errors."""
    errors: list[str] = []
    if process.poll() is None:
        process.terminate()
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                output, _ = process.communicate(timeout=10)
            except subprocess.TimeoutExpired as exc:
                errors.append(f"process did not stop after kill: {exc}")
                output = ""
    else:
        output, _ = process.communicate(timeout=10)
    if process.poll() is None:
        errors.append("process is still running")
    return output, errors


def measure_once(run: int) -> dict[str, object]:
    data_dir = Path(tempfile.mkdtemp(prefix=f"run-{run}-", dir=RUNTIME_ROOT))
    port = free_port()
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "src.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    env = os.environ.copy()
    env.update(
        {
            "PERSONAL_AGENT_TEST_MODE": "1",
            "PERSONAL_AGENT_DATA_DIR": str(data_dir),
            "OPENAI_API_KEY": "personal-agent-invalid-test-key",
            "OPENAI_BASE_URL": "https://api.deepseek.com",
            "MODEL": "deepseek-flash",
            "NTFY_SERVER": "http://127.0.0.1:1",
            "NTFY_TOPIC": "personal-agent-test-topic",
        }
    )

    result: dict[str, object] = {"run": run}
    process: subprocess.Popen[str] | None = None
    started = time.perf_counter()
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        deadline = started + READY_TIMEOUT_SECONDS
        url = f"http://127.0.0.1:{port}/health"
        while time.perf_counter() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"server exited before readiness with code {process.returncode}"
                )
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        elapsed_ms = (time.perf_counter() - started) * 1000
                        result.update(
                            {
                                "ready_ms": round(elapsed_ms, 3),
                                "http_status": response.status,
                                "data_dir_reused": False,
                            }
                        )
                        break
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.05)
        if "ready_ms" not in result:
            raise TimeoutError(
                f"/health did not return 200 within {READY_TIMEOUT_SECONDS} seconds"
            )
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        # Keep the child output available for diagnosing a failed measurement.
        if process is not None:
            output, stop_errors = stop_process(process)
            if output:
                print(f"run {run} child output:\n{output}", file=sys.stderr)
                result["child_output"] = output
            if stop_errors:
                result["stop_errors"] = stop_errors
                result["error"] = "; ".join(stop_errors)
        shutil.rmtree(data_dir, ignore_errors=True)
    return result


def main() -> int:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    try:
        for run in range(1, RUNS + 1):
            result = measure_once(run)
            results.append(result)
            if "error" in result:
                print(f"run {run}: {result['error']}", file=sys.stderr)
                break
            print(f"run {run}: {result['ready_ms']:.3f} ms")

        timings = [
            float(result["ready_ms"]) for result in results if "ready_ms" in result
        ]
        services_stopped = all("stop_errors" not in result for result in results)
        passed = (
            len(timings) == RUNS
            and services_stopped
            and all("error" not in result for result in results)
        )
        report: dict[str, object] = {
            "date": datetime.now(timezone(timedelta(hours=8))).isoformat(),
            "status": "PASS" if passed else "FAIL",
            "scope": "existing src.main:app; no sandbox platform implemented",
            "definition": (
                "Wall time from immediately before subprocess.Popen(uvicorn) "
                "to the first GET /health response with HTTP 200; five sequential "
                "runs, each with a fresh temporary data directory and a free port."
            ),
            "command": [
                ".venv/Scripts/python.exe",
                "-m",
                "uvicorn",
                "src.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "<free-port>",
                "--log-level",
                "warning",
            ],
            "ready_timeout_seconds": READY_TIMEOUT_SECONDS,
            "python": sys.version,
            "runs": results,
            "sample_count": len(timings),
            "p50_ms": round(statistics.median(timings), 3) if timings else None,
            "min_ms": round(min(timings), 3) if timings else None,
            "max_ms": round(max(timings), 3) if timings else None,
            "temporary_data_removed": True,
            "services_stopped": services_stopped,
        }
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if passed else 1
    finally:
        shutil.rmtree(RUNTIME_ROOT, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
