"""Sleep Phase 2 pilot：10 查询 × RAG / RAG+Sleep，真实模型，小预算。

默认 judge off；硬停 120k tokens / 40 次调用。只输出脱敏汇总。
"""

from __future__ import annotations

import json
import os
import sys
import shutil
import tempfile
import time

from contextlib import closing

from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.memory.sleep import apply, propose  # noqa: E402
from src.memory.store import MemoryStore  # noqa: E402
from src.memory.versions import MemoryVersions  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "memory" / "sleep_ab.json"
OUT = ROOT / "data" / "sleep_phase2_pilot.json"
MODEL = "deepseek-chat"
MAX_TOKENS = 800_000
MAX_CALLS = 400
PILOT_QUERIES = int(os.getenv("PHASE2_QUERIES", "10"))


def main() -> int:
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("OPENAI_API_KEY", "")

    if not api_key:
        raise SystemExit("OPENAI_API_KEY 未配置")

    client = OpenAI(
        api_key=api_key,
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com"),
        timeout=60.0,
        max_retries=0,
    )
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    owner = fixture["owner_id"]
    as_of = fixture["as_of"]
    queries = [query for context in fixture["contexts"] for query in context["queries"]][:PILOT_QUERIES]

    temp = Path(tempfile.mkdtemp(prefix="pa-phase2-pilot-"))
    base_path = temp / "base.db"
    base = MemoryStore(base_path)

    for spec in fixture["items"]:
        base.add_item(owner, **spec)

    snapshot = MemoryVersions(base).create("phase2-pilot")
    candidate = propose(base, owner, as_of=as_of, snapshot=snapshot)

    import sqlite3

    sleep_path = temp / "sleep.db"

    with closing(sqlite3.connect(base_path)) as origin, closing(sqlite3.connect(sleep_path)) as target:
        origin.backup(target)

    sleep_store = MemoryStore(sleep_path)
    apply(sleep_store, candidate, snapshot)

    import src.memory.store as store_module

    original_now = store_module.utc_now
    store_module.utc_now = lambda: as_of
    results = []
    calls = 0
    tokens = 0
    started = time.perf_counter()

    for spec in queries:
        expected = spec["expected_keys"][0]
        for arm, store in (("rag", base), ("sleep", sleep_store)):
            if calls >= MAX_CALLS or tokens >= MAX_TOKENS:
                break

            hits = store.search(owner, spec["query"], k=5)["hits"]
            numbered = []

            for index, hit in enumerate(hits, 1):
                numbered.append(f"[{index}] ({hit.item.key}) {hit.item.text}")

            prompt = (
                "资料如下，每条形如 [编号] (key) 内容：\n"
                + "\n".join(numbered)
                + f"\n\n问题：{spec['query']}\n"
                "如果资料能回答，answer 直接写资料中的关键词；"
                "如果资料不能回答，answer 写“不知道”。"
            )
            started_call = time.perf_counter()
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "只输出 JSON，格式："
                            '{"answer": "字符串", "citations": [1]}。'
                            "citations 是资料编号数组；不知道时 answer 为“不知道”，"
                            "citations 为空数组。不要输出 JSON 以外的任何文字。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=200,
            )
            calls += 1
            usage = getattr(response, "usage", None)
            used = int(getattr(usage, "total_tokens", 0) or 0)
            tokens += used
            raw = (response.choices[0].message.content or "").strip()
            latency = (time.perf_counter() - started_call) * 1000
            text = raw.strip()

            if text.startswith("```"):
                text = text.strip("`")
                text = text.removeprefix("json").strip()

            try:
                payload = json.loads(text)
                answer = str(payload.get("answer") or "").strip()
                citations = payload.get("citations") or []
            except (json.JSONDecodeError, AttributeError):
                answer = raw
                citations = []

            cited = [
                hits[index - 1].item.key
                for index in citations
                if isinstance(index, int) and 1 <= index <= len(hits)
            ]

            fact_tokens = set(hits[0].item.text.lower().split()) if hits else set()
            results.append({
                "query": spec["query"],
                "expected_key": expected,
                "arm": arm,
                "answer_chars": len(answer),
                "answer": answer[:120],
                "fact_hit": expected.lower() in answer.lower()
                or bool(fact_tokens & set(answer.lower().split())),
                "citation_ok": expected in cited,
                "tokens": used,
                "latency_ms": round(latency, 1),
                "top_keys": [hit.item.key for hit in hits],
            })


    store_module.utc_now = original_now
    summary = {"model": MODEL, "judge": "off", "calls": calls, "tokens": tokens, "queries": PILOT_QUERIES}

    for arm in ("rag", "sleep"):
        rows = [row for row in results if row["arm"] == arm]
        summary[arm] = {
            "n": len(rows),
            "fact_hit": round(sum(row["fact_hit"] for row in rows) / len(rows), 3) if rows else 0.0,
            "citation_ok": round(sum(row["citation_ok"] for row in rows) / len(rows), 3) if rows else 0.0,
            "avg_tokens": round(sum(row["tokens"] for row in rows) / len(rows), 1) if rows else 0.0,
            "p50_latency_ms": sorted(row["latency_ms"] for row in rows)[len(rows) // 2] if rows else 0.0,
        }

    summary["duration_s"] = round(time.perf_counter() - started, 2)
    summary["samples"] = results
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "samples"}, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
