"""知识库检索评测：known-item 查询 + recall@k / MRR / 延迟（+ 可选引用命中率）。

**方法学（如实标注，别把数字讲成它不代表的含义）**

查询从语料自身派生，属于 *known-item retrieval* 评测：
"给定一篇已知文档的特征词，能不能把它捞回来"。
它衡量检索通道本身的召回与排序能力，**不**衡量开放域问答质量。

为了不让高频词（Python、release…）把数字灌水，每个文档只取
**在整个语料里只出现过一次**（document frequency = 1）的词：
- `title` 类：标题里的稀有词
- `body` 类：摘要里、且标题中没有的稀有词

语料是 `scripts/build_knowledge_corpus.py` 抓下来的**真实**快照，
所以同一份语料任何时候跑出来的数字都可复现、可前后对比。

引用命中率（`--citations`）会真实跑一遍模型，因此**产生模型费用**；
默认关闭。判定完全走落库的检索审计 `knowledge_retrievals`：
`top_chunk_ids` 是这轮真正注入的片段顺序（即 `[n]` 的编号），
`cited_chunk_ids` 是模型回复里出现的 `[n]` 映射回来的 chunk。
于是"检索到了但模型没用"和"引用了错误的片段"能被分开看见。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\run_knowledge_eval.py
    .\\.venv\\Scripts\\python.exe scripts\\run_knowledge_eval.py --k 5
    .\\.venv\\Scripts\\python.exe scripts\\run_knowledge_eval.py --citations --citation-limit 10
"""

import argparse
import atexit
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
import uuid

from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 评测数据目录必须在导入 src.config 之前确定：
# 这样 src.agent 在请求路径上构造的 KnowledgeStore 也落在临时目录，
# 绝不会写进项目真实知识库（含 ai_usage）。
WORKDIR = Path(tempfile.mkdtemp(prefix="kb_eval_")).resolve()
os.environ["PERSONAL_AGENT_DATA_DIR"] = str(WORKDIR)

atexit.register(
    lambda: shutil.rmtree(WORKDIR, ignore_errors=True)
)

from src.knowledge import (  # noqa: E402
    KnowledgeStore,
    build_embedder,
    index_embeddings,
    ingest_upload,
    retrieve,
)


CORPUS = ROOT / "evals" / "knowledge" / "corpus.jsonl"
RESULTS = ROOT / "evals" / "knowledge" / "results"

WORD = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{2,}")
CJK = re.compile(r"[\u4e00-\u9fff]{3,}")

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "are", "was",
    "now", "here", "you", "have", "has", "from", "not", "but",
    "python", "release", "released", "available", "final",
    "team", "news", "blog", "http", "https", "www", "com",
    "lightning", "talks", "summit", "language",
}


def tokens(text: str) -> list[str]:
    """抽出可用于检索的词（英文词 ≥3 字符 / 中文连续 ≥3 字）。"""

    found = WORD.findall(text) + CJK.findall(text)

    return [
        token.lower()
        for token in found
        if token.lower() not in STOPWORDS
    ]


def load_corpus() -> list[dict]:
    rows = []

    for line in CORPUS.read_text(
        encoding="utf-8"
    ).splitlines():
        line = line.strip()

        if line:
            rows.append(json.loads(line))

    return rows


def document_frequency(rows: list[dict]) -> dict[str, int]:
    counts = {}

    for row in rows:
        for token in set(tokens(row["text"])):
            counts[token] = counts.get(token, 0) + 1

    return counts


def build_cases(rows: list[dict]) -> list[dict]:
    """为每篇文档派生 1–2 条稀有词查询。"""

    df = document_frequency(rows)
    cases = []

    for row in rows:
        title_tokens = [
            token
            for token in dict.fromkeys(tokens(row["title"]))
            if df.get(token) == 1
        ]

        body_tokens = [
            token
            for token in dict.fromkeys(
                tokens(row["text"])
            )
            if df.get(token) == 1
            and token not in set(tokens(row["title"]))
        ]

        if title_tokens:
            cases.append(
                {
                    "id": f"{row['id']}_title",
                    "category": "title",
                    "query": title_tokens[0],
                    "expect_doc": row["id"],
                }
            )

        if body_tokens:
            cases.append(
                {
                    "id": f"{row['id']}_body",
                    "category": "body",
                    "query": body_tokens[0],
                    "expect_doc": row["id"],
                }
            )

        # 混合类：稀有词 + 一个中等频率词（出现在 2~5 篇里）。
        medium = [
            token
            for token in dict.fromkeys(tokens(row["text"]))
            if 2 <= df.get(token, 0) <= 5
        ]

        if title_tokens and medium:
            cases.append(
                {
                    "id": f"{row['id']}_mixed",
                    "category": "mixed",
                    "query": f"{title_tokens[0]} {medium[0]}",
                    "expect_doc": row["id"],
                }
            )

    return cases


def percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0

    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        int(round(ratio * (len(ordered) - 1))),
    )

    return ordered[index]


def run_citation_eval(
    *,
    cases: list[dict],
    store: KnowledgeStore,
    doc_ids: dict,
    limit: int,
    owner_id: str = "eval_owner",
) -> dict:
    """真实跑一遍模型，量"检索到了，模型有没有用上"。

    会产生模型费用；调用方必须已经确认愿意承担。
    """

    from unittest.mock import patch

    from src import agent
    from src.core.ai_usage import AIUsageStore
    from src.core.reminder_identity import (
        bind_authenticated_reminder,
    )

    usage = AIUsageStore(
        store.db_path.parent / "ai_usage.db"
    )

    results = []
    cited_count = 0
    hit_count = 0
    latencies = []

    with patch.object(
        agent, "_get_usage_store", return_value=usage
    ):
        for case in cases[:limit]:
            request_id = "eval" + uuid.uuid4().hex
            started = time.perf_counter()

            with bind_authenticated_reminder(
                user_id=owner_id,
                request_id=request_id,
                role="owner",
            ):
                with agent.track_chat_usage(
                    owner_id, request_id
                ):
                    reply = agent.chat(case["query"])

            latency_ms = int(
                (time.perf_counter() - started) * 1000
            )
            latencies.append(latency_ms)

            audit = store.list_retrievals(
                owner_id, limit=1
            )[0]

            cited_chunks = audit["cited_chunk_ids"]
            documents = store.chunk_documents(cited_chunks)

            cited_docs = {
                doc_ids.get(document_id)
                for document_id in documents.values()
                if doc_ids.get(document_id) is not None
            }

            expected = case["expect_doc"]
            has_citation = bool(cited_chunks)
            hit = expected in cited_docs

            if has_citation:
                cited_count += 1

            if hit:
                hit_count += 1

            results.append(
                {
                    "id": case["id"],
                    "query": case["query"],
                    "expect_doc": expected,
                    "cited_chunk_ids": cited_chunks,
                    "cited_docs": sorted(cited_docs),
                    "hit": hit,
                    "latency_ms": latency_ms,
                    "reply": reply[:400],
                }
            )

    total = len(results)
    usage_summary = usage.user_summary(owner_id)

    return {
        "model": agent.MODEL,
        "cases": total,
        "cited_rate": (
            round(cited_count / total, 4) if total else 0.0
        ),
        "citation_hit_rate": (
            round(hit_count / total, 4) if total else 0.0
        ),
        "p95_latency_ms": percentile(latencies, 0.95),
        "observed_tokens": (
            usage_summary["observed_total_tokens"]
        ),
        "results": results,
    }


def evaluate(
    *,
    cases: list[dict],
    store: KnowledgeStore,
    doc_ids: dict,
    k: int,
    mode: str,
    embedder=None,
) -> dict:
    """按一种检索模式跑完全部查询，返回 summary 与逐条结果。"""

    hit_at = {1: 0, 3: 0, 5: 0, 10: 0}
    reciprocal = []
    latencies = []
    channels = {}
    results = []

    for case in cases:
        outcome = retrieve(
            owner_id="eval_owner",
            query=case["query"],
            k=k,
            mode=mode,
            store=store,
            embedder=embedder,
        )

        rank = None

        for hit in outcome["hits"]:
            if doc_ids.get(
                hit["document_id"]
            ) == case["expect_doc"]:
                rank = hit["rank"]
                break

        for cutoff in hit_at:
            if rank is not None and rank <= cutoff:
                hit_at[cutoff] += 1

        reciprocal.append(
            1.0 / rank if rank else 0.0
        )
        latencies.append(outcome["latency_ms"])

        channel = (
            outcome["channels"].get("bm25")
            or outcome["channels"].get("vector")
            or mode
        )
        channels[channel] = channels.get(channel, 0) + 1

        results.append(
            {
                "id": case["id"],
                "category": case["category"],
                "query": case["query"],
                "expect_doc": case["expect_doc"],
                "rank": rank,
                "mode": mode,
                "channel": channel,
                "latency_ms": outcome["latency_ms"],
            }
        )

    total = len(cases)
    by_category = {}

    for case in results:
        bucket = by_category.setdefault(
            case["category"], {"total": 0, "hit@5": 0}
        )
        bucket["total"] += 1

        if case["rank"] is not None and case["rank"] <= 5:
            bucket["hit@5"] += 1

    return {
        "mode": mode,
        "summary": {
            "queries": total,
            "k": k,
            "recall_at_1": round(hit_at[1] / total, 4),
            "recall_at_3": round(hit_at[3] / total, 4),
            "recall_at_5": round(hit_at[5] / total, 4),
            "recall_at_10": round(hit_at[10] / total, 4),
            "mrr": round(
                statistics.fmean(reciprocal), 4
            ),
            "p50_latency_ms": percentile(
                latencies, 0.5
            ),
            "p95_latency_ms": percentile(
                latencies, 0.95
            ),
            "channels": channels,
            "by_category": {
                name: {
                    "queries": bucket["total"],
                    "recall_at_5": round(
                        bucket["hit@5"] / bucket["total"],
                        4,
                    ),
                }
                for name, bucket in sorted(
                    by_category.items()
                )
            },
        },
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--corpus", default=str(CORPUS))
    parser.add_argument(
        "--mode",
        choices=("bm25", "vector", "hybrid"),
        default="bm25",
        help="检索模式（默认 bm25，即 R2 的基线）",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="同一批查询跑 bm25 / vector / hybrid 三组并对照",
    )
    parser.add_argument(
        "--embedding-provider",
        choices=("hashing", "openai", "local"),
        default=None,
        help="向量来源：hashing（词法，离线）/ openai / local",
    )
    parser.add_argument(
        "--citations",
        action="store_true",
        help="额外真实跑一遍模型，量引用命中率（产生费用）",
    )
    parser.add_argument(
        "--citation-limit",
        type=int,
        default=10,
        help="引用评测最多跑多少条查询（控制费用）",
    )
    args = parser.parse_args()

    rows = load_corpus()

    if not rows:
        print("语料为空：先跑 scripts/build_knowledge_corpus.py")
        return 1

    cases = build_cases(rows)

    if not cases:
        print("没有派生任何查询（语料里的稀有词不足）")
        return 1

    # 评测永远在临时库里跑：绝不碰真实知识库
    store = KnowledgeStore(WORKDIR / "knowledge.db")

    doc_ids = {}

    for row in rows:
        result = ingest_upload(
            owner_id="eval_owner",
            filename=row["id"],
            text=row["text"],
            store=store,
        )
        doc_ids[result["id"]] = row["id"]

    embedding_needed = (
        args.compare or args.mode in ("vector", "hybrid")
    )

    embedder = None

    if embedding_needed:
        embedder = build_embedder(args.embedding_provider)

        indexed = index_embeddings(
            owner_id="eval_owner",
            store=store,
            embedder=embedder,
        )

        print(
            "  embeddings  %s（新算 %d 段 / 命中缓存 %d 段）"
            % (
                embedder.name,
                indexed["embedded"],
                indexed["cached"],
            )
        )

        if embedder.name.startswith("hashing"):
            print(
                "  ！HashingEmbedder 是**词法**向量，不是语义向量："
                "它只证明链路能跑，别把这些数字当成语义检索的效果"
            )

    modes_to_run = (
        ["bm25", "vector", "hybrid"]
        if args.compare
        else [args.mode]
    )

    reports = [
        evaluate(
            cases=cases,
            store=store,
            doc_ids=doc_ids,
            k=args.k,
            mode=mode,
            embedder=embedder,
        )
        for mode in modes_to_run
    ]

    primary = reports[0]
    summary = primary["summary"]
    results = primary["cases"]
    total = len(cases)

    citation = None

    if args.citations:
        if not os.getenv("OPENAI_API_KEY"):
            print("")
            print("没有 OPENAI_API_KEY，跳过引用评测")
        else:
            citation = run_citation_eval(
                cases=cases,
                store=store,
                doc_ids=doc_ids,
                limit=args.citation_limit,
            )

    RESULTS.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    target = RESULTS / f"eval_{stamp}.json"

    payload = {
        "started_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "strategy": "+".join(modes_to_run),
        "embedder": (
            embedder.name if embedder is not None else None
        ),
        "corpus": Path(args.corpus).name,
        "corpus_documents": len(rows),
        "summary": summary,
        "comparison": [
            {
                "mode": report["mode"],
                **report["summary"],
            }
            for report in reports
        ],
        "cases": results,
    }

    if citation is not None:
        payload["citations"] = citation

    target.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("")
    print("Knowledge retrieval eval (known-item)")
    print("=" * 56)
    print(
        "  corpus      %d documents (%s)"
        % (len(rows), Path(args.corpus).name)
    )
    print("  queries     %d" % total)
    print("  mode        %s" % primary["mode"])
    print("  channel     %s" % summary["channels"])
    print("-" * 56)
    print("  recall@1    %.4f" % summary["recall_at_1"])
    print("  recall@3    %.4f" % summary["recall_at_3"])
    print("  recall@5    %.4f" % summary["recall_at_5"])
    print("  recall@10   %.4f" % summary["recall_at_10"])
    print("  MRR         %.4f" % summary["mrr"])
    print(
        "  latency     p50 %d ms / p95 %d ms"
        % (
            summary["p50_latency_ms"],
            summary["p95_latency_ms"],
        )
    )

    for name, bucket in summary["by_category"].items():
        print(
            "  %-11s recall@5 %.4f (%d queries)"
            % (name, bucket["recall_at_5"], bucket["queries"])
        )

    if args.compare:
        print("-" * 56)
        print(
            "  %-8s %-9s %-9s %-9s %-9s %s"
            % (
                "mode",
                "recall@1",
                "recall@5",
                "MRR",
                "p95 ms",
                "channels",
            )
        )

        for report in reports:
            row = report["summary"]

            print(
                "  %-8s %-9.4f %-9.4f %-9.4f %-9d %s"
                % (
                    report["mode"],
                    row["recall_at_1"],
                    row["recall_at_5"],
                    row["mrr"],
                    row["p95_latency_ms"],
                    row["channels"],
                )
            )

    if citation is not None:
        print("-" * 56)
        print(
            "  citations   %d cases (%s)"
            % (citation["cases"], citation["model"])
        )
        print(
            "  cited%%      %.4f"
            % citation["cited_rate"]
        )
        print(
            "  cite-hit%%   %.4f"
            % citation["citation_hit_rate"]
        )
        print(
            "  tokens      %d"
            % citation["observed_tokens"]
        )

    print("=" * 56)
    print("  result file %s" % target.relative_to(ROOT))
    print("")

    return 0


if __name__ == "__main__":
    sys.exit(main())
