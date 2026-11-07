"""跑一次 ResearchGraph，产出报告与证据包。

两种模式：

- `--live`：真实检索 + 真实模型，按路线图 W2 判据（≥3000 字 / ≥10 来源）；
- `--offline`（默认）：脚本化模型 + 固定证据，用于验证链路与**逐字节复现**。
  离线模式**不冒充字数达标**，门槛降为 300 字 / 2 来源，只证明流程可跑通。

退出码：0 = 通过；1 = 失败或不成立（引用缺失等）；3 = 产物成立但未达判据（partial）。

证据默认落 `<workspace>/pa-w2-evidence-20261107/`；报告 Markdown 与运行 JSON 同名。
"""

from __future__ import annotations

import argparse
import json
import sys

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.research.graph import DONE, ResearchGraph, SearchRoundResult  # noqa: E402
from src.research.llm import LLMCall, LLMUsage  # noqa: E402
from src.research.report import (  # noqa: E402
    MIN_CHARS,
    MIN_SOURCES,
    check_report,
    count_chars,
    render_report,
)
from src.research.store import ResearchStore, reset_store  # noqa: E402
from src.web.evidence import EvidenceStore, reset_store as reset_evidence  # noqa: E402


DEFAULT_OUT = (
    Path(r"C:\Users\123\Documents\deepseek-harness\default-workspace")
    / "pa-w2-evidence-20261107"
)

OFFLINE_MIN_CHARS = 300
OFFLINE_MIN_SOURCES = 2


# --------------------------------------------------------------------------
# 离线脚本：一次"有缺口 → 补研 → 评审 → 重写 → 通过"的固定演出
# --------------------------------------------------------------------------

OFFLINE_EVIDENCE = (
    (
        "https://arxiv.org/abs/2309.06180",
        "PagedAttention：Efficient Memory Management for LLM Serving",
        "PagedAttention 把 KV cache 切成固定大小的块，块在显存中不必连续，"
        "逻辑顺序由页表维护。这样预留的空间几乎都能装真实 token 而不是填充，"
        "内部碎片被压到接近零。论文在同一硬件与延迟目标下对比强基线，"
        "报告了显著更高的吞吐，并指出长序列与输出长度可变时收益最大。",
    ),
    (
        "https://docs.vllm.ai/en/latest/",
        "vLLM 文档：分页注意力与内存管理",
        "vLLM 是基于 PagedAttention 的推理服务引擎，支持连续批处理与"
        "前缀共享。前缀共享让多个请求复用同一段缓存的物理块，从而进一步"
        "降低显存占用。文档同时说明实现依赖具体硬件与模型配置，"
        "部署前应按自身负载重新测量吞吐与延迟。",
    ),
    (
        "https://www.sqlite.org/wal.html",
        "SQLite Write-Ahead Logging",
        "WAL 模式把修改先写入日志文件，读操作不阻塞写、写不阻塞读。"
        "该页说明了检查点机制与何时需要显式 checkpoint，以及 WAL 在"
        "多进程访问下的行为边界。",
    ),
)

OFFLINE_PLAN = {
    "title_hint": "PagedAttention 与 vLLM 的显存管理",
    "sub_queries": [
        {"query": "PagedAttention vLLM arxiv", "why": "原始论文"},
        {"query": "vLLM documentation memory management", "why": "官方文档"},
        {"query": "KV cache fragmentation inference serving", "why": "量化收益"},
        {"query": "paged attention implementation serving engine", "why": "工程落地"},
    ],
}

OFFLINE_ANALYST_GAP = {
    "findings": [
        {
            "claim": "PagedAttention 用固定大小的块管理 KV cache，块可非连续存放。",
            "evidence_ids": [1],
        },
        {
            "claim": "vLLM 在前缀共享上复用物理块，进一步降低显存占用。",
            "evidence_ids": [2],
        },
    ],
    "gaps": [
        {
            "gap": "缺少真实生产环境的显存占用对比",
            "query": "vLLM production memory comparison",
        }
    ],
}

OFFLINE_ANALYST_OK = {
    "findings": [
        {
            "claim": "PagedAttention 用固定大小的块管理 KV cache，块可非连续存放。",
            "evidence_ids": [1],
        },
        {
            "claim": "vLLM 在前缀共享上复用物理块，进一步降低显存占用。",
            "evidence_ids": [2],
        },
        {
            "claim": "论文报告的吞吐收益依赖硬件与负载，迁移前需重新测量。",
            "evidence_ids": [1, 2],
        },
    ],
    "gaps": [],
}

OFFLINE_WRITER_DRAFT = {
    "title": "PagedAttention 与 vLLM 的显存管理",
    "summary": "本报告基于一手论文与官方文档，说明分页式 KV cache 的组织方式、"
    "它带来的吞吐收益及其成立条件，并明确标注尚未取得生产实测数据的局限。",
    "sections": [
        {
            "heading": "问题背景",
            "paragraphs": [
                {
                    "text": "大模型推理服务需要让一个批次内的请求同时驻留在显存里，"
                    "而每个请求的注意力键值缓存会随生成过程持续增长。传统做法按最大"
                    "长度预留连续显存，于是产生内部与外部碎片，实际可用空间被压低。"
                    "论文把这种浪费归因于缓存的组织方式，而不是模型参数规模本身，"
                    "这一判断决定了后续优化的方向是改变存放结构。",
                    "evidence_ids": [1],
                }
            ],
        },
        {
            "heading": "机制",
            "paragraphs": [
                {
                    "text": "PagedAttention 把缓存切成固定大小的块，块在显存里不必连续，"
                    "逻辑顺序由页表维护。这样预留的空间几乎都能装真实 token，"
                    "填充造成的浪费被压到很低。在同一硬件与延迟目标下，"
                    "基于该机制的 vLLM 吞吐明显高于强基线，长序列与输出长度可变时"
                    "收益最大。需要强调的是，这些数字来自论文的测量条件，"
                    "迁移到其他硬件与负载前应当重新测量。",
                    "evidence_ids": [1, 2],
                }
            ],
        },
        {
            "heading": "局限",
            "paragraphs": [
                {
                    "text": "本报告没有取得真实生产环境的显存占用对比数据，"
                    "因此不对生产收益作定量承诺。部署前应按自身负载重新测量吞吐与"
                    "延迟，并确认所用版本的行为与论文一致。前缀共享的收益也依赖"
                    "请求之间是否存在可复用的公共前缀，负载不满足这一前提时收益会下降。",
                    "evidence_ids": [1, 2],
                }
            ],
        },
    ],
    "limitations": "缺少生产环境实测数据；论文之外的实现版本可能存在行为差异。",
}

OFFLINE_CRITIC_REVISE = {
    "score": 6,
    "verdict": "revise",
    "issues": [
        {
            "claim": "机制一节",
            "reason": "缺少对测量条件的说明，结论有外推风险",
            "action": "补充论文的硬件与负载条件，并标注结论不能直接外推",
        }
    ],
}

OFFLINE_CRITIC_PASS = {"score": 9, "verdict": "pass", "issues": []}


class ScriptedLLM:
    """离线模式：按阶段吐出固定 payload。"""

    def __init__(self, script):
        self.script = {stage: list(items) for stage, items in script.items()}
        self.calls = []

    def __call__(self, messages, *, stage, max_tokens):
        self.calls.append(stage)
        queue = self.script.get(stage)

        if not queue:
            raise AssertionError(f"离线脚本里没有阶段 {stage} 的响应")

        item = queue.pop(0)

        return item, LLMCall(
            text="{}",
            usage=LLMUsage(0, 0, 0),
            latency_ms=0.0,
            attempts=1,
            model="offline-script",
        )


def seed_offline_evidence(store: EvidenceStore) -> tuple[int, ...]:
    ids = []

    for url, title, text in OFFLINE_EVIDENCE:
        record = store.add_source(url=url, text=text, title=title)
        ids.append(record.id)

    return tuple(ids)


def make_offline_search(evidence_ids):
    def search(queries, round_index):
        # 两轮都返回同一批证据：离线模式只验证链路，不验证"新证据增量"
        return SearchRoundResult(
            queries=tuple(queries), evidence_ids=tuple(evidence_ids), failures=0
        )

    return search


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def _slug(text: str) -> str:
    keep = [char for char in text if char.isalnum() or char in "-_"]
    slug = "".join(keep)[:40]

    return slug or "research"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="跑一次 ResearchGraph 并产出报告")
    parser.add_argument("--query", required=True, help="研究问题")
    parser.add_argument("--live", action="store_true", help="真实检索 + 真实模型")
    parser.add_argument("--offline", action="store_true", help="离线脚本模式（默认）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="证据输出目录")
    parser.add_argument("--min-chars", type=int, default=None)
    parser.add_argument("--min-sources", type=int, default=None)
    parser.add_argument("--max-rounds", type=int, default=2)
    parser.add_argument("--max-revisions", type=int, default=2)
    parser.add_argument("--max-llm-calls", type=int, default=16)
    parser.add_argument("--evidence-db", default="", help="证据库路径（临时目录用）")
    parser.add_argument("--research-db", default="", help="研究账本路径（临时目录用）")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    live = bool(args.live) and not args.offline

    if live:
        min_chars = MIN_CHARS if args.min_chars is None else args.min_chars
        min_sources = MIN_SOURCES if args.min_sources is None else args.min_sources
    else:
        min_chars = OFFLINE_MIN_CHARS if args.min_chars is None else args.min_chars
        min_sources = OFFLINE_MIN_SOURCES if args.min_sources is None else args.min_sources

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.evidence_db:
        evidence = EvidenceStore(Path(args.evidence_db))

        import src.web.evidence as evidence_module

        evidence_module._STORE = evidence
        evidence_module._STORE_PATH = evidence.db_path

    else:
        reset_evidence()
        from src.web.evidence import get_evidence_store

        evidence = get_evidence_store()

    if args.research_db:
        research_store = ResearchStore(Path(args.research_db))
    else:
        reset_store()
        from src.research.store import get_research_store

        research_store = get_research_store()

    if live:
        graph = ResearchGraph(
            args.query,
            evidence_store=evidence,
            store=research_store,
            max_research_rounds=args.max_rounds,
            max_revisions=args.max_revisions,
            max_llm_calls=args.max_llm_calls,
        )
    else:
        seeded = seed_offline_evidence(evidence)

        graph = ResearchGraph(
            args.query,
            llm=ScriptedLLM(
                {
                    "planner": [OFFLINE_PLAN],
                    "analyst": [OFFLINE_ANALYST_GAP, OFFLINE_ANALYST_OK],
                    "writer": [OFFLINE_WRITER_DRAFT, OFFLINE_WRITER_DRAFT],
                    "critic": [OFFLINE_CRITIC_REVISE, OFFLINE_CRITIC_PASS],
                }
            ),
            search_round=make_offline_search(seeded),
            evidence_store=evidence,
            store=research_store,
            max_research_rounds=args.max_rounds,
            max_revisions=args.max_revisions,
            max_llm_calls=args.max_llm_calls,
        )

    result = graph.run()

    if result.draft is None:
        print(f"FAIL state={result.state} error={result.error}")

        return 1

    report = render_report(
        result.draft,
        evidence,
        question=args.query,
        min_chars=min_chars,
        min_sources=min_sources,
    )

    missing = check_report(report.markdown, evidence)
    slug = _slug(args.query)
    stamp = datetime.now(timezone.utc)
    md_path = out_dir / f"{slug}.md"
    json_path = out_dir / f"{slug}.json"

    md_path.write_text(report.markdown, encoding="utf-8")

    if missing:
        research_store.update_run(
            result.run_id,
            state="failed",
            error="引用指向不存在的证据",
        )

        print(f"FAIL 引用缺失：{[f'E{item}' for item in missing]}")
        json_path.write_text(
            json.dumps(
                {
                    "mode": "live" if live else "offline",
                    "state": result.state,
                    "missing_ids": list(missing),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        return 1

    research_store.update_run(
        result.run_id,
        report_path=str(md_path),
        report_chars=report.char_count,
    )

    payload = {
        "generated_at": stamp.isoformat(),
        "mode": "live" if live else "offline",
        "query": args.query,
        "run_id": result.run_id,
        "state": result.state,
        "ok": result.ok,
        "research_rounds": result.research_rounds,
        "revision_count": result.revision_count,
        "search_failures": result.search_failures,
        "llm_calls": result.llm_calls,
        "usage": asdict(result.usage),
        "evidence_ids": list(result.evidence_ids),
        "critic": (
            None
            if result.critic is None
            else {
                "score": result.critic.score,
                "verdict": result.critic.verdict,
                "issues": [issue.claim for issue in result.critic.issues],
            }
        ),
        "report": {
            "chars": report.char_count,
            "sources": report.source_count,
            "min_chars": min_chars,
            "min_sources": min_sources,
            "problems": list(report.problems),
            "passed": report.passed,
            "missing_ids": list(missing),
            "markdown_chars": count_chars(report.markdown),
        },
        "error": result.error,
        "events": [
            {
                "seq": event.seq,
                "from": event.from_state,
                "to": event.to_state,
                "actor": event.actor,
                "reason": event.reason,
            }
            for event in result.events
        ],
    }

    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        f"state={result.state} 轮数={result.research_rounds} "
        f"重写={result.revision_count} llm_calls={result.llm_calls} "
        f"chars={report.char_count} sources={report.source_count}"
    )

    if result.events:
        print("迁移：" + " → ".join(event.to_state for event in result.events))

    print(f"报告：{md_path}")
    print(f"证据：{json_path}")

    if result.state == DONE and report.passed:
        print("PASS")
        return 0

    print(
        "PARTIAL "
        f"state={result.state} problems={'; '.join(report.problems) or result.error}"
    )

    return 3


if __name__ == "__main__":
    raise SystemExit(main())
