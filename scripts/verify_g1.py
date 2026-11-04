"""G1 可复跑本地证据：真实助手编排和提醒存储，模型传输使用确定性替身。

不访问真实模型或通知服务，不读取真实凭据，所有数据库都在临时目录。
输出明确标注 model_transport，不能把它当作真实模型调用证据。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def chunk(*, text=None, finish=None, tool_calls=None, usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=text, tool_calls=tool_calls or []),
            finish_reason=finish,
        )],
        usage=usage,
    )


def tool_delta(*, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=0, id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def main():
    with tempfile.TemporaryDirectory(prefix="personal-agent-g1-") as temp:
        os.environ["PERSONAL_AGENT_DATA_DIR"] = temp
        os.environ["PERSONAL_AGENT_TEST_MODE"] = "1"
        os.environ["OPENAI_API_KEY"] = "invalid-g1-test-key"
        os.environ["OPENAI_BASE_URL"] = "https://api.deepseek.com"
        os.environ["MODEL"] = "deepseek-flash"
        os.environ["NTFY_SERVER"] = "http://127.0.0.1:1"
        os.environ["NTFY_TOPIC"] = "g1-test"

        from src import agent
        from src.agent_stream import run_chat_stream
        from src.core.reminder_identity import bind_authenticated_reminder
        from src.core.run_control import CancellationToken
        from src.core.run_store import RunStore

        first_stream = iter([
            chunk(tool_calls=[tool_delta(call_id="g1-call", name="create_reminder")]),
            chunk(tool_calls=[tool_delta(arguments='{"message":"喝水","minutes":5}')], finish="tool_calls"),
            SimpleNamespace(choices=[], usage=SimpleNamespace(
                prompt_tokens=10, completion_tokens=4, total_tokens=14
            )),
        ])
        final_stream = iter([
            chunk(text="已为你创建"),
            chunk(text=" 5 分钟后的喝水提醒。", finish="stop"),
            SimpleNamespace(choices=[], usage=SimpleNamespace(
                prompt_tokens=20, completion_tokens=8, total_tokens=28
            )),
        ])
        events = []
        with bind_authenticated_reminder(
            user_id="g1-local-user", request_id="g1_local_request", role="member"
        ):
            with patch.object(
                agent.client.chat.completions, "create",
                side_effect=[first_stream, final_stream],
            ):
                reply = run_chat_stream(
                    "五分钟后提醒我喝水",
                    emit=events.append,
                    cancel_token=CancellationToken(),
                )
            reminders = agent.list_reminders()

        conn = RunStore().connect()
        try:
            row = conn.execute(
                "SELECT id FROM agent_runs WHERE request_id = 'g1_local_request'"
            ).fetchone()
        finally:
            conn.close()
        run = RunStore().get_run(row["id"], owner_id="g1-local-user")
        steps = run["turns"][0]["steps"]
        tool_step = next(step for step in steps if step["kind"] == "tool")
        assert reply == "已为你创建 5 分钟后的喝水提醒。"
        assert len(reminders) == 1
        assert reminders[0]["message"] == "喝水"
        assert run["status"] == "succeeded"
        assert tool_step["exec"]["status"] == "succeeded"
        assert tool_step["exec"]["tool_name"] == "create_reminder"

        print(json.dumps({
            "status": "PASS",
            "model_transport": "deterministic_local_sdk_double",
            "real_external_model_called": False,
            "question": "五分钟后提醒我喝水",
            "event_sequence": [event["type"] for event in events],
            "answer": reply,
            "reminders_created_in_temporary_db": len(reminders),
            "run_status": run["status"],
            "exec_status": tool_step["exec"]["status"],
            "tool_name": tool_step["exec"]["tool_name"],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
