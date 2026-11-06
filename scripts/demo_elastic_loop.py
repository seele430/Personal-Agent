r"""W3 演示：一条可重复的弹性闭环（隔离数据目录里真注册/真摘除节点）。

跑法（必须先把数据目录指到临时目录，脚本会自己检查）：

    $env:PERSONAL_AGENT_DATA_DIR = "$env:TEMP\pa-elastic-demo"
    .\.venv\Scripts\python.exe scripts\demo_elastic_loop.py

它做的事，就是 V2 §10 要的那条链路：

    队列负载上升 → 控制器算出更高的期望容量 → 真的注册节点
    → 负载回落 → 冷却允许后真的 drain + remove → 事件落库可复查

不是仿真：用的是真实的 ExecQueue、NodeRegistry、platform_scale_events。
"""

from __future__ import annotations

import os
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def require_isolated_data_dir() -> Path:
    raw = os.getenv("PERSONAL_AGENT_DATA_DIR", "").strip()

    if not raw:
        fail(
            "必须先设置 PERSONAL_AGENT_DATA_DIR 到临时目录，"
            "否则会往项目真实 platform.db 里写节点和事件"
        )

    path = Path(raw).expanduser().resolve()

    if path == ROOT.resolve():
        fail("PERSONAL_AGENT_DATA_DIR 不能是项目根目录")

    if "temp" not in str(path).lower() and not path.name.startswith("pa-"):
        fail(f"数据目录看起来不像临时目录：{path}")

    path.mkdir(parents=True, exist_ok=True)

    return path


def main() -> int:
    data_dir = require_isolated_data_dir()

    from src.platform.autoscale import AutoscalePolicy
    from src.platform.elastic_controller import ElasticController
    from src.platform.exec_queue import ExecQueue, QueuedExec
    from src.platform.node_registry import NodeRegistry
    from src.platform.scale_events import ScaleEventStore

    print(f"data_dir = {data_dir}")

    queue = ExecQueue(max_active=0, max_queued=200, max_wait_seconds=0.0)
    registry = NodeRegistry()
    store = ScaleEventStore()

    policy = AutoscalePolicy(
        min_nodes=1,
        max_nodes=4,
        capacity_per_node=8,
        headroom=0.0,
        scale_up_after=1,
        scale_down_cooldown_seconds=0.0,
    )

    controller = ElasticController(
        policy=policy,
        registry=registry,
        store=store,
        queue=queue,
        apply=True,
    )

    issued: list[str] = []

    def submit(count: int) -> None:
        for index in range(count):
            exec_id = f"demo-{len(issued):04d}"
            queue.submit(
                QueuedExec(
                    exec_id=exec_id,
                    sandbox_id="sbx-demo",
                    argv=["echo", "hi"],
                    timeout_ms=1000,
                    max_output_bytes=1024,
                )
            )
            issued.append(exec_id)

    def clear() -> None:
        for exec_id in list(issued):
            queue.cancel(exec_id)
        issued.clear()

    def tick(label: str, now: float) -> dict:
        decision = controller.tick(now=now)
        capacity = controller.capacity()
        print(
            f"{label:<12} queued={decision['queued']:<3} "
            f"desired={decision['desired_capacity']} "
            f"actual={decision['actual_capacity']} "
            f"action={decision['action']:<11} "
            f"applied={str(decision['applied']):<5} "
            f"blocked={decision['blocked'] or '-'} "
            f"after={capacity['nodes_accepting']}"
        )
        return decision

    print("\n== 阶段 1：低负载 ==")
    submit(1)
    tick("load-low", now=0.0)

    print("\n== 阶段 2：负载上升 ==")
    submit(20)

    for step in range(4):
        tick(f"load-high#{step + 1}", now=10.0 + step)

    print("\n== 阶段 3：负载回落 ==")
    clear()

    for step in range(3):
        tick(f"load-idle#{step + 1}", now=100.0 + step)

    print("\n== 结果 ==")

    events = store.recent(20)
    order = list(reversed(events))

    for event in order:
        print(
            f"  {event['at']}  {event['action']:<10} "
            f"{event['before_capacity']} -> {event['after_capacity']}  "
            f"reason={event['reason']}  source={event['source']}"
        )

    actions = [event["action"] for event in order]
    ups = actions.count("scale_up")
    downs = actions.count("scale_down")
    capacity = controller.capacity()

    print(
        f"\n  events={len(events)} (scale_up={ups}, scale_down={downs})  "
        f"nodes_total={capacity['nodes_total']}  "
        f"accepting={capacity['nodes_accepting']}  "
        f"removed={capacity['nodes_total'] - capacity['nodes_accepting']}"
    )

    print("\n== 断言 ==")

    if ups != 3:
        fail(f"期望 3 次扩容（0->1->2->3），实际 {ups}")

    if downs != 2:
        fail(f"期望 2 次缩容（3->2->1），实际 {downs}")

    if capacity["nodes_accepting"] != 1:
        fail(f"最终可用容量应为 1，实际 {capacity['nodes_accepting']}")

    reopened = ScaleEventStore()

    if reopened.count() != len(events):
        fail("事件没有持久化：重新打开存储层后条数不一致")

    snapshot = controller.snapshot(limit=10)

    if snapshot["event_count"] != len(events):
        fail("控制器快照与事件表条数不一致")

    if snapshot["last_decision"]["action"] != "none":
        fail("最后一轮负载已回落且容量到位，应当是 hold")

    print("  OK: 3 次扩容、2 次缩容、事件持久化、最终收敛到 1 个节点")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
