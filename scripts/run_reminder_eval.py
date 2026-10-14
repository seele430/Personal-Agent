"""提醒工具 eval 运行器。

会真实调用 DeepSeek，产生费用，所以：
- 默认只做干跑（加载并校验用例，不联网）
- 必须显式传 --confirm-cost 才会真正调用
- 预估费用超过 --max-cost 时直接拒绝运行

只做第 1 次模型调用（tool_choice="auto"），不执行提醒工具，
不设置用量上下文，因此不写 ai_usage.db / reminders.db /
chat_requests.db，也不占用任何用户的预算额度。

参考时间固定，保证"半小时后"这类用例的期望值不随运行时间漂移。
prompt 来自 agent.build_system_prompt，与线上同一份。

用法：
    python scripts/run_reminder_eval.py
    python scripts/run_reminder_eval.py --confirm-cost
    python scripts/run_reminder_eval.py --confirm-cost --only rel_01,abs_01
"""

import argparse
import hashlib
import json
import sys

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src import agent  # noqa: E402
from src.evals.reminder_eval import (  # noqa: E402
    extract_tool_calls,
    grade,
    load_cases,
)


CASES_PATH = BASE_DIR / "evals" / "reminder" / "cases.jsonl"
RESULTS_DIR = BASE_DIR / "evals" / "reminder" / "results"

# 固定参考时间：2026-10-12 周一 12:00，东八区。
REFERENCE_TIME = datetime(
    2026, 10, 12, 12, 0,
    tzinfo=timezone(timedelta(hours=8)),
)

# deepseek-flash 费率（美元 / 百万 token）。
# 与 src/core/ai_pricing.py 的登记价一致，仅用于本脚本的费用显示。
RATE_INPUT_MISS = 0.30
RATE_INPUT_HIT = 0.006
RATE_OUTPUT = 1.20

DEFAULT_MAX_COST_USD = 0.10


def token_cost_usd(prompt, cached, completion) -> float:
    miss = max((prompt or 0) - (cached or 0), 0)

    return (
        miss * RATE_INPUT_MISS
        + (cached or 0) * RATE_INPUT_HIT
        + (completion or 0) * RATE_OUTPUT
    ) / 1_000_000


def usage_of(response):
    usage = getattr(response, "usage", None)

    def pick(name):
        value = getattr(usage, name, None)
        return value if type(value) is int and value >= 0 else None

    cached = pick("prompt_cache_hit_tokens")

    if cached is None:
        details = getattr(usage, "prompt_tokens_details", None)
        value = getattr(details, "cached_tokens", None)
        cached = value if type(value) is int and value >= 0 else None

    return {
        "prompt_tokens": pick("prompt_tokens"),
        "cached_tokens": cached,
        "completion_tokens": pick("completion_tokens"),
    }


def worst_case_cost_usd(count, prompt_estimate=600) -> float:
    """按"输入全未命中缓存 + 输出打满"估算上限。"""

    return count * token_cost_usd(
        prompt_estimate,
        0,
        agent.MAX_OUTPUT_TOKENS,
    )


def run_single(case):
    """single 策略：一次 tool_choice="auto" 调用。"""

    agent.validate_user_input(case.input)

    messages = [
        {
            "role": "system",
            "content": agent.build_system_prompt(REFERENCE_TIME),
        },
        {
            "role": "user",
            "content": case.input,
        },
    ]

    # 复用线上调用路径（工具定义、max_tokens、thinking 开关一致）。
    response = agent._completion(messages, tool_choice="auto")
    message = response.choices[0].message

    return message, response


def evaluate(cases, strategy):
    records = []

    for index, case in enumerate(cases, start=1):
        print(f"[{index}/{len(cases)}] {case.id} ...", end=" ", flush=True)

        record = {
            "id": case.id,
            "category": case.category,
            "expect": case.expect,
        }

        try:
            if strategy != "single":
                raise NotImplementedError(f"未实现的策略：{strategy}")

            message, response = run_single(case)

        except Exception as error:
            record.update(
                passed=False,
                reason=f"调用失败：{type(error).__name__}: {error}",
                usage=None,
                cost_usd=0.0,
                first_round=None,
                final=None,
            )
            records.append(record)
            print("ERROR")
            continue

        calls = extract_tool_calls(message)
        result = grade(case, calls)
        usage = usage_of(response)

        # 为后续对比辩论 / 批评者策略预留：
        # single 策略下 first_round 与 final 相同。
        summary = [
            {"name": call.name, "arguments": call.arguments}
            for call in calls
        ]

        record.update(
            passed=result.passed,
            reason=result.reason,
            usage=usage,
            cost_usd=round(
                token_cost_usd(
                    usage["prompt_tokens"],
                    usage["cached_tokens"],
                    usage["completion_tokens"],
                ),
                8,
            ),
            first_round=summary,
            final=summary,
        )

        records.append(record)
        print("PASS" if result.passed else "FAIL")

    return records


def report(records):
    by_category = defaultdict(lambda: [0, 0])

    for record in records:
        bucket = by_category[record["category"]]
        bucket[1] += 1

        if record["passed"]:
            bucket[0] += 1

    passed = sum(1 for record in records if record["passed"])
    total = len(records)
    cost = sum(record["cost_usd"] for record in records)

    print()
    print("按类别：")

    for category in sorted(by_category):
        ok, count = by_category[category]
        print(f"  {category:<14} {ok}/{count}")

    print()
    print(f"总通过率：{passed}/{total}")
    print(f"实际花费：US${cost:.6f}")

    failures = [record for record in records if not record["passed"]]

    if failures:
        print()
        print("未通过：")

        for record in failures:
            print(f"  {record['id']}: {record['reason']}")

    return {
        "passed": passed,
        "total": total,
        "cost_usd": round(cost, 8),
        "by_category": {
            category: {"passed": ok, "total": count}
            for category, (ok, count) in sorted(by_category.items())
        },
    }


def previous_run():
    if not RESULTS_DIR.is_dir():
        return None

    files = sorted(RESULTS_DIR.glob("eval_*.json"))

    if not files:
        return None

    with open(files[-1], encoding="utf-8") as file:
        return files[-1].name, json.load(file)


def compare(records):
    earlier = previous_run()

    if earlier is None:
        print()
        print("没有历史结果，本次作为基线。")
        return

    name, data = earlier
    old = {item["id"]: item["passed"] for item in data.get("cases", [])}

    regressed = []
    fixed = []

    for record in records:
        before = old.get(record["id"])

        if before is None:
            continue

        if before and not record["passed"]:
            regressed.append(record["id"])
        elif not before and record["passed"]:
            fixed.append(record["id"])

    print()
    print(f"对比 {name}：")
    print(f"  退步 {len(regressed)}：{regressed or '无'}")
    print(f"  修复 {len(fixed)}：{fixed or '无'}")


def save(records, summary, strategy):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    prompt = agent.build_system_prompt(REFERENCE_TIME)

    payload = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model": agent.MODEL,
        "strategy": strategy,
        "reference_time": REFERENCE_TIME.isoformat(),
        "prompt_sha256": hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest()[:16],
        "summary": summary,
        "cases": records,
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"eval_{stamp}.json"

    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")

    print()
    print(f"结果已保存：{path.relative_to(BASE_DIR)}")


def main():
    parser = argparse.ArgumentParser(
        description="提醒工具 eval（默认干跑，不调用模型）"
    )
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--strategy", default="single", choices=["single"])
    parser.add_argument("--only", default="", help="逗号分隔的用例 id")
    parser.add_argument(
        "--max-cost",
        type=float,
        default=DEFAULT_MAX_COST_USD,
        help=f"预估花费上限，美元（默认 {DEFAULT_MAX_COST_USD}）",
    )
    parser.add_argument(
        "--confirm-cost",
        action="store_true",
        help="真正调用模型并产生费用",
    )

    args = parser.parse_args()

    cases = load_cases(args.cases)

    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        unknown = wanted - {case.id for case in cases}

        if unknown:
            sys.exit(f"未知用例 id：{sorted(unknown)}")

        cases = [case for case in cases if case.id in wanted]

    estimate = worst_case_cost_usd(len(cases))

    print(f"用例数：{len(cases)}")
    print(f"模型：{agent.MODEL}")
    print(f"参考时间：{REFERENCE_TIME.isoformat()}")
    print(f"策略：{args.strategy}")
    print(f"预估花费上限：US${estimate:.4f}")

    if estimate > args.max_cost:
        sys.exit(
            f"预估 US${estimate:.4f} 超过上限 US${args.max_cost:.4f}，已中止。"
            " 可用 --only 缩小范围，或显式提高 --max-cost。"
        )

    if not args.confirm_cost:
        print()
        print("干跑完成：用例格式有效，未调用模型，未产生费用。")
        print("真正运行请加 --confirm-cost。")
        return

    if not getattr(agent.client, "api_key", None):
        sys.exit("未配置 API Key，已中止。")

    print()
    records = evaluate(cases, args.strategy)
    summary = report(records)
    compare(records)
    save(records, summary, args.strategy)


if __name__ == "__main__":
    main()