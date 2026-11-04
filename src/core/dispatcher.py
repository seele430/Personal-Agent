"""主 Agent 调度：分解 → 匹配专长 → 执行 → 汇总。

设计要点（和固定流水线不同）：
1. **动态分解**：子任务数量与依赖由主 agent 当场决定。
2. **专长匹配**：按 skill 选人，选不到就降级给第一个成员。
3. **失败重派**：先重试一次，再失败则标记，依赖它的任务跳过。

对外只有两个可注入口子（planner / executor），
测试时传假函数，生产时走模型。
"""

import json

import contextvars

import logging

import re

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from src.core.trace_context import span, start_span


logger = logging.getLogger("personal_agent.dispatch")


MAX_SUBTASKS = 8

MAX_CONTEXT_CHARS = 4000


class DispatchError(Exception):
    """调度失败（给 API 转 500/422）。"""


@dataclass(frozen=True)
class SubTaskPlan:

    title: str
    skill: str = ""
    depends_on: tuple[int, ...] = ()


# ==========================================
# 选人
# ==========================================

def pick_agent(agents: list[dict], skill: str) -> dict | None:
    """专长精确匹配；匹配不到返回 None（由调用方降级）。"""

    if not agents:
        return None

    if not skill:
        return agents[0]

    normalized = skill.strip().lower()

    for agent in agents:
        skills = [item.lower() for item in agent.get("skills", [])]

        if normalized in skills:
            return agent

    return None


# ==========================================
# 默认实现：走模型
# ==========================================

def plan_with_llm(goal: str, agents: list[dict]) -> list[SubTaskPlan]:

    from src import agent as agent_module

    skills = sorted({
        skill
        for item in agents
        for skill in item.get("skills", [])
    })

    roster = "\n".join(
        f"- {item['name']}: {', '.join(item.get('skills', []))}"
        for item in agents
    )

    prompt = f"""你是任务调度者。把目标拆成 2-{MAX_SUBTASKS} 个子任务。

成员与专长：
{roster}

可用专长标签：{', '.join(skills) if skills else '（无）'}

规则：
1. 每个子任务只能交给一个成员，用 skill 字段指定专长标签。
2. depends_on 填前置子任务的下标（从 0 开始），没有依赖就填空数组。
3. 依赖只能指向**更靠前**的下标，不能成环。
4. 只输出 JSON 数组，不要任何解释、不要代码块标记。

格式：
[{{"title": "子任务标题", "skill": "专长标签", "depends_on": []}}]

目标：{goal}
"""

    response = agent_module.client.chat.completions.create(
        model=agent_module.MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )

    text = response.choices[0].message.content or ""

    return parse_plan(text)


def parse_plan(text: str) -> list[SubTaskPlan]:
    """容错解析：去掉代码块标记，取第一个 JSON 数组。"""

    cleaned = text.strip()

    cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    start = cleaned.find("[")
    end = cleaned.rfind("]")

    if start == -1 or end == -1 or end < start:
        raise DispatchError("主 agent 没有返回 JSON 数组")

    try:
        raw = json.loads(cleaned[start:end + 1])

    except json.JSONDecodeError as error:
        raise DispatchError(f"计划不是合法 JSON：{error}") from None

    if not isinstance(raw, list) or not raw:
        raise DispatchError("计划为空")

    plans: list[SubTaskPlan] = []

    for index, item in enumerate(raw[:MAX_SUBTASKS]):
        if not isinstance(item, dict):
            continue

        title = str(item.get("title", "")).strip()

        if not title:
            continue

        depends = item.get("depends_on") or []

        if not isinstance(depends, list):
            depends = []

        # 只接受更靠前的下标，天然防环
        valid = tuple(
            int(dep)
            for dep in depends
            if isinstance(dep, int) and 0 <= dep < index
        )

        plans.append(SubTaskPlan(
            title=title,
            skill=str(item.get("skill", "")).strip(),
            depends_on=valid,
        ))

    if not plans:
        raise DispatchError("计划里没有有效子任务")

    return plans


def execute_with_llm(
    agent_profile: dict,
    task: dict,
    context: str,
) -> str:

    from src import agent as agent_module

    prompt = f"""你是 {agent_profile['name']}，专长：{', '.join(agent_profile.get('skills', []))}。

当前任务：{task['title']}

前序任务产出（可能为空）：
{context[:MAX_CONTEXT_CHARS] or '（无）'}

要求：直接给出这项任务的结果，简洁、可执行，不要复述任务本身。
"""

    response = agent_module.client.chat.completions.create(
        model=agent_profile.get("model") or agent_module.MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )

    return (response.choices[0].message.content or "").strip()


# ==========================================
# 调度主循环
# ==========================================

def run_goal(
    store,
    team_id: int,
    goal: str,
    planner=None,
    executor=None,
) -> dict:
    """入口：包一层根 span，让任务树和 span 树一一对应。"""

    handle = start_span(
        "dispatch.goal",
        team_id=team_id,
        goal_chars=len(goal),
    )

    try:
        tree = _run_goal(
            store,
            team_id,
            goal,
            planner=planner,
            executor=executor,
        )

    except Exception:
        handle.end("error")
        raise

    handle.end("ok" if tree.get("status") == "done" else "error")

    return tree


def _run_goal(
    store,
    team_id: int,
    goal: str,
    planner=None,
    executor=None,
) -> dict:
    """同步跑完一个目标，返回任务树。"""

    team = store.get_team(team_id)

    if team is None:
        raise DispatchError("团队不存在")

    agents = store.list_agents(team_id)

    if not agents:
        raise DispatchError("团队里还没有成员")

    planner = planner or plan_with_llm
    executor = executor or execute_with_llm

    root = store.create_task(
        team_id=team_id,
        title=goal[:200],
        skill="root",
        status="running",
        input_text=goal,
    )

    with span("dispatch.plan", team_id=team_id, goal_chars=len(goal)):
        plans = planner(goal, agents)

    # 建子任务：下标记号 → 真实 id
    created = []

    for plan in plans:
        depends = [
            created[dep]["id"]
            for dep in plan.depends_on
            if dep < len(created)
        ]

        task = store.create_task(
            team_id=team_id,
            title=plan.title,
            skill=plan.skill,
            parent_id=root["id"],
            depends_on=depends,
        )

        created.append(task)

    outputs: dict[int, str] = {}

    def run_one(task: dict) -> tuple[int, str, str | None]:
        """返回 (task_id, status, error)。"""

        profile = pick_agent(agents, task.get("skill") or "")

        if profile is None:
            # 降级：交给第一个成员，并留痕
            profile = agents[0]

            store.add_event(
                task["id"],
                "fallback",
                {"reason": "no_agent_for_skill", "skill": task.get("skill")},
            )

        store.update_task(
            task["id"],
            status="running",
            agent_id=profile["id"],
        )

        store.add_event(
            task["id"],
            "dispatched",
            {"agent": profile["name"]},
        )

        context = "\n\n".join(
            outputs[dep]
            for dep in task["depends_on"]
            if dep in outputs
        )

        last_error = None

        # 失败重派：最多两次尝试
        for attempt in range(1, 3):
            try:
                with span(
                    "task.run",
                    task_id=task["id"],
                    skill=task.get("skill") or "",
                    agent=profile["name"],
                    attempt=attempt,
                ):
                    result = executor(profile, task, context)

                store.update_task(
                    task["id"],
                    status="done",
                    output=result,
                    attempts=attempt,
                    error=None,
                )

                store.add_event(task["id"], "succeeded", {"attempt": attempt})

                return task["id"], "done", None

            except Exception as error:  # noqa: BLE001 - 单任务失败不能拖垮整队
                last_error = str(error)

                logger.exception(
                    "子任务失败 task_id=%s attempt=%s",
                    task["id"],
                    attempt,
                )

        store.update_task(
            task["id"],
            status="failed",
            error=last_error,
            attempts=2,
        )

        store.add_event(task["id"], "failed", {"error": last_error})

        return task["id"], "failed", last_error

    pending = {task["id"]: task for task in created}

    failed: set[int] = set()

    while pending:
        ready = [
            task
            for task in pending.values()
            if all(dep in outputs for dep in task["depends_on"])
            and not any(dep in failed for dep in task["depends_on"])
        ]

        if not ready:
            # 要么有环，要么依赖了失败节点
            for task in pending.values():
                blocked = any(dep in failed for dep in task["depends_on"])

                store.update_task(
                    task["id"],
                    status="skipped" if blocked else "failed",
                    error="依赖未完成",
                )

                store.add_event(
                    task["id"],
                    "skipped" if blocked else "failed",
                    {"reason": "blocked"},
                )

            break

        # 线程池不会自动复制 contextvar：每个任务带一份主线程上下文，
        # 这样 task.run 才能挂到 dispatch.goal 下面。
        contexts = [contextvars.copy_context() for _ in ready]

        with ThreadPoolExecutor(
            max_workers=max(1, int(team.get("max_workers") or 1))
        ) as pool:
            results = list(
                pool.map(
                    lambda pair: pair[0].run(run_one, pair[1]),
                    zip(contexts, ready),
                )
            )

        for task_id, status, _error in results:
            pending.pop(task_id, None)

            if status == "done":
                task = store.get_task(task_id)
                outputs[task_id] = task.get("output") or ""

            else:
                failed.add(task_id)

    # 根任务汇总：直接拼子任务产出，不再多调一次模型
    summary = "\n\n".join(
        f"## {store.get_task(task_id)['title']}\n{text}"
        for task_id, text in outputs.items()
    )

    root_done = len(failed) == 0 and all(
        store.get_task(task["id"])["status"] == "done"
        for task in created
    )

    store.update_task(
        root["id"],
        status="done" if root_done else "failed",
        output=summary,
        error=None if root_done else "部分子任务失败",
    )

    store.add_event(
        root["id"],
        "finished",
        {"ok": root_done, "children": len(created)},
    )

    return store.task_tree(root["id"]) or root
