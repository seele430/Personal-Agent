"""团队任务分配：存储 + 调度核心用例。

不联网、不调用模型：planner / executor 全部注入假函数。
"""

import tempfile

import unittest

from pathlib import Path

from src.core.dispatcher import (
    DispatchError,
    SubTaskPlan,
    parse_plan,
    pick_agent,
    run_goal,
)
from src.core.team_store import TeamError, TeamStore


class TeamStoreTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TeamStore(Path(self.tmp.name) / "teams.db")

        self.team = self.store.create_team(
            owner_id="u1", name="测试团队", max_workers=1,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_team_and_agents(self):
        agent = self.store.add_agent(
            team_id=self.team["id"],
            name="研究员",
            skills=["research", "search"],
        )

        self.assertEqual(agent["skills"], ["research", "search"])

        agents = self.store.list_agents(self.team["id"])

        self.assertEqual(len(agents), 1)

    def test_agent_requires_skill(self):
        with self.assertRaises(TeamError):
            self.store.add_agent(
                team_id=self.team["id"],
                name="空技能",
                skills=["  "],
            )

    def test_task_tree_nests_children(self):
        root = self.store.create_task(
            team_id=self.team["id"], title="根任务",
        )

        child = self.store.create_task(
            team_id=self.team["id"],
            title="子任务",
            parent_id=root["id"],
        )

        self.store.create_task(
            team_id=self.team["id"],
            title="孙任务",
            parent_id=child["id"],
        )

        tree = self.store.task_tree(root["id"])

        self.assertEqual(tree["title"], "根任务")
        self.assertEqual(len(tree["children"]), 1)
        self.assertEqual(
            tree["children"][0]["children"][0]["title"], "孙任务",
        )

    def test_update_rejects_unknown_field(self):

        task = self.store.create_task(
            team_id=self.team["id"], title="x",
        )

        with self.assertRaises(TeamError):
            self.store.update_task(task["id"], title="改名")


class PlanningTests(unittest.TestCase):

    def test_parse_plan_accepts_code_fence(self):

        text = '```json\n[{"title": "查资料", "skill": "research", "depends_on": []}]\n```'

        plans = parse_plan(text)

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].skill, "research")

    def test_parse_plan_drops_forward_dependency(self):
        """依赖只能指向更靠前的下标，否则丢弃——天然防环。"""

        text = (
            '[{"title": "A", "skill": "", "depends_on": [1]},'
            ' {"title": "B", "skill": "", "depends_on": []}]'
        )

        plans = parse_plan(text)

        self.assertEqual(plans[0].depends_on, ())

    def test_parse_plan_rejects_garbage(self):
        with self.assertRaises(DispatchError):
            parse_plan("我不想做这个任务")

    def test_pick_agent_matches_skill(self):

        agents = [
            {"id": 1, "name": "A", "skills": ["write"]},
            {"id": 2, "name": "B", "skills": ["research"]},
        ]

        self.assertEqual(pick_agent(agents, "research")["id"], 2)
        self.assertIsNone(pick_agent(agents, "unknown"))
        self.assertEqual(pick_agent(agents, "")["id"], 1)


class DispatchTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TeamStore(Path(self.tmp.name) / "teams.db")

        self.team = self.store.create_team(
            owner_id="u1", name="小队", max_workers=1,
        )

        self.store.add_agent(
            team_id=self.team["id"], name="研究员", skills=["research"],
        )

        self.store.add_agent(
            team_id=self.team["id"], name="写手", skills=["write"],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_goal_respects_dependency_order(self):

        order = []

        def planner(goal, agents):
            return [
                SubTaskPlan("查资料", "research"),
                SubTaskPlan("写初稿", "write", (0,)),
            ]

        def executor(profile, task, context):
            order.append(task["title"])
            self.assertIn(profile["name"], ("研究员", "写手"))
            return f"{task['title']} 的结果"

        tree = run_goal(
            self.store,
            self.team["id"],
            "写一份报告",
            planner=planner,
            executor=executor,
        )

        self.assertEqual(order, ["查资料", "写初稿"])
        self.assertEqual(tree["status"], "done")
        self.assertEqual(len(tree["children"]), 2)

        first = tree["children"][0]

        self.assertEqual(first["status"], "done")
        self.assertIn("查资料 的结果", first["output"])

    def test_failure_retries_then_skips_dependents(self):

        calls = {"count": 0}

        def planner(goal, agents):
            return [
                SubTaskPlan("会失败的任务", "research"),
                SubTaskPlan("依赖它的任务", "write", (0,)),
            ]

        def executor(profile, task, context):
            calls["count"] += 1

            if task["title"] == "会失败的任务":
                raise RuntimeError("模型超时")

            return "ok"

        tree = run_goal(
            self.store,
            self.team["id"],
            "必失败",
            planner=planner,
            executor=executor,
        )

        # 失败任务重试一次 = 2 次调用；依赖它的任务直接跳过
        self.assertEqual(calls["count"], 2)

        failed, skipped = tree["children"]

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["attempts"], 2)

        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(tree["status"], "failed")

    def test_dispatch_records_events(self):

        def planner(goal, agents):
            return [SubTaskPlan("唯一任务", "research")]

        def executor(profile, task, context):
            return "done"

        tree = run_goal(
            self.store,
            self.team["id"],
            "单任务",
            planner=planner,
            executor=executor,
        )

        child = tree["children"][0]

        kinds = [event["kind"] for event in self.store.events(child["id"])]

        self.assertIn("created", kinds)
        self.assertIn("dispatched", kinds)
        self.assertIn("succeeded", kinds)


if __name__ == "__main__":
    unittest.main()
