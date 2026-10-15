"""知识库检索评测：known-item 查询 + recall@k / MRR / 延迟。

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

用法：
    .\\.venv\\Scripts\\python.exe scripts\\run_knowledge_eval.py
    .\\.venv\\Scripts\\python.exe scripts\\run_knowledge_eval.py --k 5
"""

import argparse
import json
import re
import statistics
import sys
import tempfile

from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.knowledge import KnowledgeStore, ingest_upload, retrieve


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
        # 单稀有词查询几乎"一对一"，太容易；混入有竞争的词之后，
        # 排序能力才真的被考验到。
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument(
        "--corpus", default=str(CORPUS)
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
    workdir = Path(tempfile.mkdtemp(prefix="kb_eval_"))
    store = KnowledgeStore(workdir / "knowledge.db")

    doc_ids = {}

    for row in rows:
        result = ingest_upload(
            owner_id="eval_owner",
            filename=row["id"],
            text=row["text"],
            store=store,
        )
        doc_ids[result["id"]] = row["id"]

    hit_at = {1: 0, 3: 0, 5: 0, 10: 0}
    reciprocal = []
    latencies = []
    modes = {}
    results = []

    for case in cases:
        outcome = retrieve(
            owner_id="eval_owner",
            query=case["query"],
            k=args.k,
            store=store,
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
        modes[outcome["mode"]] = (
            modes.get(outcome["mode"], 0) + 1
        )

        results.append(
            {
                "id": case["id"],
                "category": case["category"],
                "query": case["query"],
                "expect_doc": case["expect_doc"],
                "rank": rank,
                "mode": outcome["mode"],
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

    summary = {
        "queries": total,
        "corpus_documents": len(rows),
        "k": args.k,
        "recall_at_1": round(hit_at[1] / total, 4),
        "recall_at_3": round(hit_at[3] / total, 4),
        "recall_at_5": round(hit_at[5] / total, 4),
        "recall_at_10": round(hit_at[10] / total, 4),
        "mrr": round(statistics.fmean(reciprocal), 4),
        "p50_latency_ms": percentile(latencies, 0.5),
        "p95_latency_ms": percentile(latencies, 0.95),
        "channels": modes,
        "by_category": {
            name: {
                "queries": bucket["total"],
                "recall_at_5": round(
                    bucket["hit@5"] / bucket["total"], 4
                ),
            }
            for name, bucket in sorted(
                by_category.items()
            )
        },
    }

    RESULTS.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    target = RESULTS / f"eval_{stamp}.json"

    target.write_text(
        json.dumps(
            {
                "started_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "strategy": "bm25-trigram",
                "corpus": Path(args.corpus).name,
                "summary": summary,
                "cases": results,
            },
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
    print("  channel     %s" % modes)
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

    print("=" * 56)
    print("  result file %s" % target.relative_to(ROOT))
    print("")

    return 0


if __name__ == "__main__":
    sys.exit(main())
