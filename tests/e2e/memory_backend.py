"""测试专用后端：真实 FastAPI/审批/runner/SQLite，模型用 mock。

启动前必须设置 PA_TEST_DATA 指向临时数据目录；监听 8000，供 Playwright 使用。
"""

from __future__ import annotations

import json
import os
import sys

from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

data_dir = os.environ.get("PA_TEST_DATA", "")

if not data_dir:
    raise SystemExit("PA_TEST_DATA 未设置")

os.environ["PERSONAL_AGENT_DATA_DIR"] = data_dir

from src import agent  # noqa: E402
from src.config import paths  # noqa: E402
from src.core.auth_store import AuthStore  # noqa: E402


class _Usage:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0


def _tool_message(text: str, key: str):
    arguments = json.dumps({"text": text, "key": key}, ensure_ascii=False)
    return SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="call_memory_test",
                type="function",
                function=SimpleNamespace(
                    name="memory_remember", arguments=arguments
                ),
            )
        ],
    )


calls = {"count": 0}


def fake_completion(messages, tool_choice="auto", tools=None, on_delta=None, cancel_token=None):
    calls["count"] += 1

    if calls["count"] % 2 == 1:
        message = _tool_message("测试记住：12 月 1 日交项目", "e2e-deadline")
    else:
        message = SimpleNamespace(content="已记住。", tool_calls=[])

    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=_Usage(),
        model="mock-memory",
    )


agent._completion = fake_completion

store = AuthStore(paths.auth_users_db)

if not store.has_users():
    store.bootstrap_owner("owner", "test-password-123")

import uvicorn  # noqa: E402

from src.main import app  # noqa: E402


uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
