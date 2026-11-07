"""D27/D29：平台压测（开放式到达率，固定 RPS 阶梯）。

真服务、真子进程、固定 RPS 发车：每个阶梯按 ``t0 + i/rps`` 的节奏提交 exec，
不因为上一条慢就往后拖（开放式到达率）。统计提交/成功/拒绝/延迟分位与
期间队列峰值，产出 CSV，供 saturation 报告使用。

用法：

    .\\venv\\Scripts\\python.exe scripts\\bench_platform.py --ladder 2,4,8,12 --duration 15

注意：单机 + 默认 max_active=4，RPS 高到一定程度会被队列挡住，
这是**预期**的饱和信号，不是脚本坏了。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BENCH_DIR = ROOT / "docs" / "bench"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(port: int, env: dict, log_path: Path) -> subprocess.Popen:
    log = log_path.open("w+", encoding="utf-8")
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "src.main:app",
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def wait_health(port: int, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 60

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("服务提前退出")

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2
            ) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)

    raise RuntimeError("服务启动超时")


def request(port, method, path, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0

    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def run_stage(
    port: int,
    sandbox_id: str,
    rps: int,
    duration: float,
    *,
    server_pid: int | None = None,
    repeat: int = 0,
) -> dict:
    total = max(1, int(rps * duration))
    interval = 1.0 / rps
    latencies: list[float] = []
    statuses: dict[str, int] = {}
    queue_peak = 0
    lock = threading.Lock()

    samples: list[tuple[float, float]] = []  # (cpu_percent, rss_mb)
    stop_sampling = threading.Event()

    def sample_resources() -> None:
        if server_pid is None:
            return

        try:
            import psutil
        except ImportError:
            return

        try:
            root = psutil.Process(server_pid)
        except psutil.Error:
            return

        # Windows 上 venv 的 python.exe 可能是启动器：真正的 uvicorn 在子进程里。
        # 采样时把 python 进程树聚合起来，避免只测到 5 MB 的壳。
        targets = [root]

        try:
            children = [
                child
                for child in root.children(recursive=True)
                if child.name().lower().startswith("python")
            ]

            if children:
                targets = children
        except psutil.Error:
            pass

        for process in targets:
            try:
                process.cpu_percent(interval=None)
            except psutil.Error:
                pass

        while not stop_sampling.wait(2.0):
            cpu_total = 0.0
            rss_total = 0.0

            for process in targets:
                try:
                    cpu_total += process.cpu_percent(interval=None)
                    rss_total += process.memory_info().rss / 1e6
                except psutil.Error:
                    continue

            samples.append((cpu_total, rss_total))

    sampler = threading.Thread(target=sample_resources, daemon=True)

    def one(index: int) -> None:
        nonlocal queue_peak
        started = time.monotonic()
        status, body = request(
            port,
            "POST",
            f"/v1/sandboxes/{sandbox_id}/exec",
            {"command": [sys.executable, "-c", "print(1)"], "timeout_ms": 30000},
        )

        if status != 202:
            code = body.get("error", {}).get("code", str(status))
            with lock:
                statuses[code] = statuses.get(code, 0) + 1

                # 429 的 body 里可能是 QUEUE_FULL 等具体 code；两种口径都记，
                # 否则 rejected_429 永远为 0。
                if status == 429:
                    statuses["429"] = statuses.get("429", 0) + 1
            return

        with lock:
            statuses["202"] = statuses.get("202", 0) + 1

        exec_id = body["exec_id"]

        while time.monotonic() - started < 120:
            code, payload = request(port, "GET", f"/v1/exec/{exec_id}")

            if code == 200 and payload["state"] in {
                "succeeded", "failed", "cancelled", "timed_out",
            }:
                with lock:
                    latencies.append(time.monotonic() - started)
                return

            time.sleep(0.03)

        with lock:
            statuses["poll_timeout"] = statuses.get("poll_timeout", 0) + 1

    began = time.monotonic()
    sampler.start()

    with ThreadPoolExecutor(max_workers=max(8, rps * 2)) as pool:
        futures = []

        for index in range(total):
            target = began + index * interval
            delay = target - time.monotonic()

            if delay > 0:
                time.sleep(delay)

            futures.append(pool.submit(one, index))

            if index % max(1, rps) == 0:
                _, queue = request(port, "GET", "/v1/queue")
                queue_peak = max(queue_peak, queue.get("queued", 0))

        for future in futures:
            future.result()

    elapsed = time.monotonic() - began
    stop_sampling.set()
    sampler.join(timeout=5)

    cpu_values = [item[0] for item in samples]
    rss_values = [item[1] for item in samples]

    return {
        "rps_target": rps,
        "repeat": repeat,
        "cpu_avg_percent": round(statistics.mean(cpu_values), 1) if cpu_values else None,
        "cpu_max_percent": round(max(cpu_values), 1) if cpu_values else None,
        "rss_avg_mb": round(statistics.mean(rss_values), 1) if rss_values else None,
        "rss_max_mb": round(max(rss_values), 1) if rss_values else None,
        "submitted": total,
        "elapsed_seconds": round(elapsed, 2),
        "achieved_rps": round(len(latencies) / elapsed, 2) if elapsed else 0.0,
        "ok": statuses.get("202", 0),
        "rejected_429": statuses.get("429", 0),
        "other": sum(
            count for key, count in statuses.items()
            if key not in {"202", "429"}
        ),
        "p50_ms": round(percentile(latencies, 50) * 1000, 1),
        "p95_ms": round(percentile(latencies, 95) * 1000, 1),
        "p99_ms": round(percentile(latencies, 99) * 1000, 1),
        "max_ms": round(max(latencies) * 1000, 1) if latencies else 0.0,
        "queue_peak": queue_peak,
        "codes": dict(sorted(statuses.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ladder", default="2,4,8,12")
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--minutes", type=float, default=None, help="覆盖 duration（分钟）")
    parser.add_argument("--repeat", type=int, default=1, help="每档重复次数")
    parser.add_argument("--base-url", default=None, help="跳过自启，直接打已有服务")
    args = parser.parse_args()

    if args.minutes:
        args.duration = args.minutes * 60

    ladder = [int(item) for item in args.ladder.split(",") if item.strip()]
    BENCH_DIR.mkdir(parents=True, exist_ok=True)

    with_sandbox_root = BENCH_DIR / "sandboxes"

    env = os.environ.copy()
    env.update(
        {
            "PERSONAL_AGENT_DATA_DIR": str(BENCH_DIR / "data"),
            "PERSONAL_AGENT_SANDBOX_ROOT": str(with_sandbox_root),
            "PERSONAL_AGENT_TEST_MODE": "1",
            "PERSONAL_AGENT_PLATFORM_REAPER": "0",
            "PERSONAL_AGENT_PLATFORM_MAX_ACTIVE_EXECS": "4",
            "PERSONAL_AGENT_PLATFORM_QUEUE_SIZE": "16",
            # 压测要测「执行容量」，不是「限流额度」：轮询会把速率放大十几倍，
            # 所以把限流调高，让 RATE_LIMITED 不参与曲线。
            "PERSONAL_AGENT_PLATFORM_RATE_PER_MIN": "1000000",
            "PERSONAL_AGENT_PLATFORM_RATE_BURST": "10000",
            # 压测直接打匿名 /v1；R6 之后需要显式声明。
            "PERSONAL_AGENT_ALLOW_ANON_V1": "1",
            "OPENAI_API_KEY": "invalid-local-test-key",
            "OPENAI_BASE_URL": "https://api.deepseek.com",
            "MODEL": "deepseek-flash",
            "NTFY_SERVER": "http://127.0.0.1:1",
            "NTFY_TOPIC": "bench-topic",
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
        }
    )

    (BENCH_DIR / "data").mkdir(parents=True, exist_ok=True)

    port = free_port()
    server = None

    if args.base_url:
        port = int(args.base_url.rsplit(":", 1)[1])
    else:
        server = start_server(port, env, BENCH_DIR / "bench-server.log")

    try:
        wait_health(port, server) if server else None

        status, sandbox = request(
            port,
            "POST",
            "/v1/sandboxes",
            {"tier": "tier1", "workspace_id": "ws_bench", "ttl_seconds": 3600},
            headers={"Idempotency-Key": f"bench-{int(time.time())}"},
        )

        if status != 201:
            raise RuntimeError(f"建沙箱失败：{status} {sandbox}")

        sandbox_id = sandbox["sandbox_id"]
        rows = []

        print(f"{'RPS':>4} {'submitted':>9} {'ok':>4} {'429':>4} "
              f"{'p50ms':>7} {'p95ms':>7} {'p99ms':>7} {'q_peak':>6}")

        for rep in range(max(1, args.repeat)):
            for rps in ladder:
                row = run_stage(
                    port,
                    sandbox_id,
                    rps,
                    args.duration,
                    server_pid=server.pid if server else None,
                    repeat=rep,
                )
                rows.append(row)
                print(
                    f"{row['rps_target']:>4} {row['submitted']:>9} {row['ok']:>4} "
                    f"{row['rejected_429']:>4} {row['p50_ms']:>7} {row['p95_ms']:>7} "
                    f"{row['p99_ms']:>7} {row['queue_peak']:>6} "
                    f"cpu={row['cpu_avg_percent']} rss={row['rss_avg_mb']}"
                )

        stamp = time.strftime("%Y%m%d-%H%M%S")
        csv_path = BENCH_DIR / f"ladder-{stamp}.csv"
        json_path = BENCH_DIR / f"ladder-{stamp}.json"

        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

        json_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        print(f"\nCSV : {csv_path}")
        print(f"JSON: {json_path}")
        return 0

    finally:
        if server is not None and server.poll() is None:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(server.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
