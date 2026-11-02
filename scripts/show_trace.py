"""查看最近 trace，或渲染某一棵 span 树。

用法：
    python scripts/show_trace.py              # 最近 20 次运行
    python scripts/show_trace.py --id <id>    # 渲染指定 trace
    python scripts/show_trace.py --limit 50   # 多看几条
"""

import argparse

import sys

from pathlib import Path

# 允许直接 python scripts/show_trace.py
sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1]),
)

from src.core.trace_store import get_trace_store  # noqa: E402


def build_children(spans: list[dict]) -> dict:

    ids = {item["span_id"] for item in spans}

    children: dict[str | None, list[dict]] = {}

    for item in spans:
        parent = item["parent_id"]

        # 父节点不在本 trace 里：当成孤儿，挂到根层面
        if parent is not None and parent not in ids:
            parent = None

        children.setdefault(parent, []).append(item)

    for group in children.values():
        group.sort(key=lambda item: item["start_ms"])

    return children


def self_ms(item: dict, children: dict) -> float:

    kids = children.get(item["span_id"], [])

    return item["dur_ms"] - sum(kid["dur_ms"] for kid in kids)


def render(
    item: dict,
    children: dict,
    prefix: str = "",
    is_last: bool = True,
    is_root: bool = True,
) -> None:

    connector = "" if is_root else ("└─ " if is_last else "├─ ")

    attrs = item.get("attrs") or {}

    attr_text = "  " + " ".join(
        f"{key}={value}" for key, value in attrs.items()
    ) if attrs else ""

    status_text = "" if item["status"] == "ok" else f"  [{item['status']}]"

    print(
        f"{prefix}{connector}{item['name']}  "
        f"{item['dur_ms']:.1f}ms (self {self_ms(item, children):.1f}ms)"
        f"{status_text}{attr_text}"
    )

    kids = children.get(item["span_id"], [])

    if is_root:
        child_prefix = prefix
    else:
        child_prefix = prefix + ("   " if is_last else "│  ")

    for index, kid in enumerate(kids):
        render(
            kid,
            children,
            child_prefix,
            index == len(kids) - 1,
            is_root=False,
        )


def show_list(limit: int) -> None:

    traces = get_trace_store().recent_traces(limit)

    if not traces:
        print("还没有任何 trace。先发一个请求，再回来看。")
        return

    print(f"{'trace_id':<34}{'span':>5}{'dur(ms)':>10}  根节点")

    for item in traces:
        print(
            f"{item['trace_id']:<34}"
            f"{item['span_count']:>5}"
            f"{item['dur_ms']:>10.1f}  {item['root_name']}"
        )


def show_trace(trace_id: str) -> None:

    spans = get_trace_store().fetch_trace(trace_id)

    if not spans:
        print(f"找不到 trace: {trace_id}")
        return

    children = build_children(spans)

    roots = children.get(None, [])

    print(f"trace {trace_id}  {len(spans)} 个 span")
    print("=" * 70)

    for index, root in enumerate(roots):
        render(root, children, is_root=True)

    print("=" * 70)

    # 孤儿提示
    ids = {item["span_id"] for item in spans}

    orphans = [
        item for item in spans
        if item["parent_id"] is not None
        and item["parent_id"] not in ids
    ]

    if orphans:
        print(f"另外还有 {len(orphans)} 个孤儿 span（父节点缺失）。")


def main() -> None:

    parser = argparse.ArgumentParser(description="查看 span 树")

    parser.add_argument("--id", dest="trace_id", default=None)
    parser.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()

    if args.trace_id:
        show_trace(args.trace_id)
    else:
        show_list(args.limit)


if __name__ == "__main__":
    main()
