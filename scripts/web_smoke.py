"""W1 真实 URL 判定：10 条预选一手页面能不能抽到正文。

路线图 §4.3 的判据在这里落成命令：

    .\\.venv\\Scripts\\python.exe scripts/web_smoke.py --live

通过标准：**10 条里至少有 8 条**抽到 ≥500 个非空白字符的正文，
且每条都带上 SHA256 与证据 ID。失败按分类统计（http_403 / timeout /
redirect / unsupported_type / extract_failed / blocked）。

离线模式（默认）不联网，用仓库里的静态夹具验证同一条链路：

    .\\.venv\\Scripts\\python.exe scripts/web_smoke.py --offline

可选 `--with-search` 会用真实搜索 Provider 打一次查询，把延迟与结果数
一起写进证据（会消耗一次搜索额度）。

证据固定落 `<workspace>/pa-w1-evidence-20261107/`，JSON 里不含任何密钥。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 独立运行时也要能读到项目 .env（应用内由 src/agent.py 负责加载）
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

except Exception:  # noqa: BLE001 - 没有 dotenv 时退回环境变量
    pass

from src.web.evidence import EvidenceError, EvidenceStore, get_evidence_store  # noqa: E402
from src.web.extract import ExtractError, extract_text  # noqa: E402
from src.web.fetch import FetchError, fetch_page  # noqa: E402
from src.web.search import SearchError, provider_from_env  # noqa: E402


OUT_DIR = (
    Path(r"C:\Users\123\Documents\deepseek-harness\default-workspace")
    / "pa-w1-evidence-20261107"
)

FIXTURES = ROOT / "tests" / "fixtures" / "web"

MIN_CHARS = 500
REQUIRED_SUCCESS = 8

#: 10 条预选一手资料（公开、无付费墙、非反爬页面）
TARGETS = (
    ("https://arxiv.org/abs/2309.06180", "vLLM / PagedAttention"),
    ("https://arxiv.org/abs/1706.03762", "Attention Is All You Need"),
    ("https://arxiv.org/abs/2005.11401", "RAG"),
    ("https://www.python.org/dev/peps/pep-0008/", "PEP 8"),
    ("https://docs.python.org/3/library/sqlite3.html", "Python sqlite3"),
    ("https://docs.python.org/3/library/unittest.html", "Python unittest"),
    ("https://www.sqlite.org/wal.html", "SQLite WAL"),
    ("https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API", "MDN Fetch API"),
    ("https://www.rfc-editor.org/rfc/rfc9110.html", "RFC 9110"),
    ("https://fastapi.tiangolo.com/tutorial/", "FastAPI Tutorial"),
)

#: 离线模式用夹具模拟"成功 2 条 + 失败 2 条"，证明成功与失败都能区分
OFFLINE_CASES = (
    ("https://arxiv.org/abs/2309.06180", "page_arxiv.html", "ok"),
    ("https://example.org/short", "page_short.html", "extract_failed"),
    ("https://example.org/broken", "page_broken.html", "extract_failed"),
)


@dataclass
class UrlResult:
    url: str
    label: str
    status: str
    http_status: int = 0
    media_type: str = ""
    method: str = ""
    char_count: int = 0
    sha256: str = ""
    evidence_id: int | None = None
    latency_ms: float = 0.0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _classify(error: Exception) -> str:
    name = type(error).__name__

    if name == "ExtractError":
        return "extract_failed"

    if name == "FetchHTTPError":
        return f"http_{getattr(error, 'status', 0)}"

    mapping = {
        "FetchTimeout": "timeout",
        "FetchBlocked": "blocked",
        "FetchTooLarge": "too_large",
        "FetchUnsupportedType": "unsupported_type",
        "FetchError": "fetch_failed",
        "EvidenceError": "evidence_error",
    }

    return mapping.get(name, "unexpected")


def judge_one(
    url: str,
    label: str,
    *,
    store: EvidenceStore,
    min_chars: int,
    body: bytes | None = None,
    media_type: str = "text/html",
) -> UrlResult:
    """抓取 + 抽取 + 入库，返回一条结构化结果（不抛异常）。"""

    started = time.perf_counter()

    try:
        if body is None:
            page = fetch_page(url)
            raw = page.body
            media = page.media_type
            http_status = page.status
            final_url = page.url
            etag = page.etag

        else:
            raw = body
            media = media_type
            http_status = 200
            final_url = url
            etag = ""

        extracted = extract_text(raw, url=final_url, min_chars=min_chars)

        record = store.add_source(
            url=url,
            final_url=final_url,
            text=extracted.text,
            http_status=http_status,
            media_type=media,
            title=extracted.title,
            method=extracted.method,
            byte_length=len(raw),
            etag=etag,
            query="web_smoke",
        )

    except Exception as error:  # noqa: BLE001 - 这里就是要归类所有失败
        return UrlResult(
            url=url,
            label=label,
            status=_classify(error),
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            detail=str(error)[:300],
        )

    return UrlResult(
        url=url,
        label=label,
        status="ok",
        http_status=http_status,
        media_type=media,
        method=extracted.method,
        char_count=record.char_count,
        sha256=record.text_sha256,
        evidence_id=record.id,
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        detail=record.title[:120],
    )


def run_live(min_chars: int, limit: int, pause: float) -> list[UrlResult]:
    store = get_evidence_store()
    results = []

    for index, (url, label) in enumerate(TARGETS[:limit], start=1):
        result = judge_one(url, label, store=store, min_chars=min_chars)
        results.append(result)
        print(
            f"[{index}/{min(limit, len(TARGETS))}] {result.status:<16} "
            f"{result.char_count:>6} 字符  {result.latency_ms:>7.0f} ms  {url}"
        )

        if pause and index < limit:
            time.sleep(pause)

    return results


def run_offline(min_chars: int) -> list[UrlResult]:
    store = get_evidence_store()
    results = []

    for url, fixture, expected in OFFLINE_CASES:
        body = (FIXTURES / fixture).read_bytes()

        result = judge_one(
            url, fixture, store=store, min_chars=min_chars, body=body
        )

        # 离线模式的"通过"= 实际结果与预期一致（成功就是成功，失败就是失败）
        result.status = "ok" if result.status == expected else "mismatch"
        results.append(result)
        print(
            f"[offline] {result.status:<16} {result.char_count:>6} 字符  "
            f"{fixture}（预期 {expected}）"
        )

    return results


def probe_search() -> dict:
    """可选：真实调用一次搜索 Provider，记录延迟与结果数。"""

    try:
        provider = provider_from_env()
        outcome = provider.search("vllm pagedattention", count=3)

        return {
            "provider": outcome.provider,
            "query": outcome.query,
            "hits": outcome.count,
            "latency_ms": outcome.latency_ms,
            "ok": True,
        }

    except SearchError as error:
        return {"ok": False, "error": str(error)[:300]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="W1 网页抽取判定")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="真实抓取 10 条 URL")
    mode.add_argument("--offline", action="store_true", help="用静态夹具（默认）")
    parser.add_argument("--min-chars", type=int, default=MIN_CHARS)
    parser.add_argument("--limit", type=int, default=len(TARGETS))
    parser.add_argument("--pause", type=float, default=1.0, help="每两条之间暂停秒数")
    parser.add_argument("--with-search", action="store_true", help="额外真实查询一次")
    parser.add_argument("--db", default="", help="证据库路径（默认用项目数据目录）")
    args = parser.parse_args(argv)

    if args.limit < 1:
        parser.error("--limit 必须 ≥ 1")

    if args.db:
        store = EvidenceStore(Path(args.db))
        import src.web.evidence as evidence_module

        evidence_module._STORE = store
        evidence_module._STORE_PATH = store.db_path

    started = datetime.now(timezone.utc)

    if args.live:
        results = run_live(args.min_chars, args.limit, args.pause)
        success = sum(1 for item in results if item.ok)
        threshold = min(REQUIRED_SUCCESS, args.limit)
        passed = success >= threshold
    else:
        results = run_offline(args.min_chars)
        success = sum(1 for item in results if item.ok)
        threshold = len(OFFLINE_CASES)
        passed = success == threshold

    search_probe = probe_search() if args.with_search else None

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    out_path = OUT_DIR / f"web-smoke-{stamp}.json"

    payload = {
        "started_at": started.isoformat(),
        "mode": "live" if args.live else "offline",
        "min_chars": args.min_chars,
        "required_success": threshold,
        "success": success,
        "total": len(results),
        "passed": passed,
        "results": [asdict(item) for item in results],
        "search_probe": search_probe,
        "python": sys.version.split()[0],
    }

    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("-" * 78)
    print(f"成功 {success}/{len(results)}（门槛 {threshold}）→ {'PASS' if passed else 'FAIL'}")
    print(f"证据：{out_path}")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
