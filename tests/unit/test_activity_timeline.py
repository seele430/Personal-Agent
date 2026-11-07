"""活动时间线测试：三源合并、排序、空态、参数校验。"""

import sqlite3
import tempfile
import unittest

from pathlib import Path

from src.activity.timeline import (
    MAX_LIMIT,
    TimelineError,
    build_timeline,
    collect_artifacts,
    collect_execs,
    collect_spans,
)


def make_spans(path: Path, rows):
    connection = sqlite3.connect(path)

    with connection:
        connection.execute(
            "CREATE TABLE spans (span_id TEXT, trace_id TEXT, parent_id TEXT, "
            "name TEXT, start_ms REAL, dur_ms REAL, status TEXT, attrs TEXT, created_at TEXT)"
        )

        for index, (created, attrs) in enumerate(rows):
            connection.execute(
                "INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?)",
                (f"s{index}", "t", None, "llm.generate", 0.0, 1.0, "ok", attrs, created),
            )

    connection.close()


def make_execs(path: Path, rows):
    connection = sqlite3.connect(path)

    with connection:
        connection.execute(
            "CREATE TABLE platform_execs (id TEXT, run_id TEXT, state TEXT, created_at TEXT)"
        )

        for index, (created, state) in enumerate(rows):
            connection.execute(
                "INSERT INTO platform_execs VALUES (?,?,?,?)",
                (f"e{index}", f"run-{index}", state, created),
            )

    connection.close()


def make_artifacts(path: Path, rows):
    connection = sqlite3.connect(path)

    with connection:
        connection.execute(
            "CREATE TABLE artifacts (id TEXT, title TEXT, kind TEXT, created_at TEXT)"
        )

        for index, (created, title) in enumerate(rows):
            connection.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?)",
                (f"a{index}", title, "report", created),
            )

    connection.close()


class TimelineCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.spans = self.root / "traces.db"
        self.platform = self.root / "platform.db"
        self.artifacts = self.root / "artifacts.db"


class TestCollectors(TimelineCase):
    def test_spans_collected_with_model_detail(self):
        make_spans(self.spans, [("2026-11-01T10:00:00", '{"model": "deepseek-flash"}')])

        events = collect_spans(self.spans, limit=10)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source, "span")
        self.assertIn("deepseek-flash", events[0].detail)

    def test_execs_collected(self):
        make_execs(self.platform, [("2026-11-01T11:00:00", "succeeded")])

        events = collect_execs(self.platform, limit=10)

        self.assertEqual(events[0].source, "exec")
        self.assertEqual(events[0].status, "succeeded")

    def test_artifacts_collected(self):
        make_artifacts(self.artifacts, [("2026-11-01T12:00:00", "某报告")])

        events = collect_artifacts(self.artifacts, limit=10)

        self.assertEqual(events[0].title, "某报告")
        self.assertEqual(events[0].source, "artifact")

    def test_missing_db_returns_empty(self):
        self.assertEqual(collect_spans(self.root / "nope.db", limit=5), [])

    def test_missing_table_returns_empty(self):
        sqlite3.connect(self.spans).close()

        self.assertEqual(collect_spans(self.spans, limit=5), [])

    def test_bad_attrs_json_is_tolerated(self):
        make_spans(self.spans, [("2026-11-01T10:00:00", "not-json")])

        self.assertEqual(collect_spans(self.spans, limit=5)[0].detail, "")


class TestBuildTimeline(TimelineCase):
    def test_merges_and_sorts_desc(self):
        make_spans(self.spans, [("2026-11-01T10:00:00", "{}")])
        make_execs(self.platform, [("2026-11-01T12:00:00", "succeeded")])
        make_artifacts(self.artifacts, [("2026-11-01T11:00:00", "报告")])

        events = build_timeline(
            traces_db=self.spans, platform_db=self.platform,
            artifacts_db=self.artifacts, limit=10,
        )

        self.assertEqual([e.source for e in events], ["exec", "artifact", "span"])

    def test_respects_limit(self):
        make_spans(
            self.spans,
            [(f"2026-11-0{index + 1}T10:00:00", "{}") for index in range(5)],
        )

        events = build_timeline(
            traces_db=self.spans, platform_db=self.platform,
            artifacts_db=self.artifacts, limit=2,
        )

        self.assertEqual(len(events), 2)

    def test_all_missing_returns_empty(self):
        events = build_timeline(
            traces_db=self.root / "a.db", platform_db=self.root / "b.db",
            artifacts_db=self.root / "c.db", limit=10,
        )

        self.assertEqual(events, [])

    def test_serializes(self):
        make_artifacts(self.artifacts, [("2026-11-01T12:00:00", "报告")])

        payload = build_timeline(
            traces_db=self.spans, platform_db=self.platform,
            artifacts_db=self.artifacts, limit=10,
        )[0].to_dict()

        self.assertEqual(payload["source"], "artifact")

    def test_limit_validation(self):
        for bad in (0, -1, MAX_LIMIT + 1, True, "5"):
            with self.subTest(value=bad):
                with self.assertRaises(TimelineError):
                    build_timeline(
                        traces_db=self.spans, platform_db=self.platform,
                        artifacts_db=self.artifacts, limit=bad,
                    )


if __name__ == "__main__":
    unittest.main()
