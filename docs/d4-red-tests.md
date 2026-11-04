# D4 多 Bot 迁移红名单

> 日期：2026-11-02
> 范围：将 `utter / host_* / debater_* / bot_lean` 及私有辅助函数从 `src/agent.py` 移至 `legacy/multi_bot.py`。正式应用已在 D3 摘除 room router，本阶段不为已下线功能补兼容别名。

## 直接相关的 7 个测试文件

执行方式：逐文件运行 `python -m unittest <module>`，原始输出见 `docs/evidence/d4-direct-red-tests.txt`。

| 文件 | 实测结果 | D5 处置 |
| --- | --- | --- |
| `tests/test_room_api.py` | 6 个用例，4 个 ERROR | 删除；独立 TTS 已由 `test_tts_api.py` 覆盖 |
| `tests/test_room_expand.py` | 3 个用例，1 个 ERROR | 删除 |
| `tests/test_room_summary.py` | 3 个用例，2 个 ERROR | 删除 |
| `tests/test_utter.py` | 6 个用例，6 个 ERROR | 删除 |
| `tests/test_room_hands.py` | 6 个用例 PASS | 删除；存储层未依赖被移函数 |
| `tests/test_room_service.py` | 13 个用例 PASS | 删除；测试通过注入 speak 避开了默认生成函数 |
| `tests/test_room_store.py` | 22 个用例 PASS | 删除；纯存储测试不依赖生成函数 |

合计 59 个直接相关用例，其中 **13 个 ERROR**。错误均指向旧 room 调用链仍查找 `src.agent.utter / debater_opening / host_summary`，属于本次下线的预期红，不通过兼容别名把它们重新接回正式助手。

## 共享保留面

`test_agent_usage_trace / test_agent_call_count / test_agent_bounds / test_agent_usage / test_bot_store / test_bot_memory / test_bot_chat / test_bot_api` 共 **71 个用例 PASS**，证据见 `docs/evidence/d4-shared-tests.txt`。

D4 不宣称全量绿；D5 删除旧 room service / store / API、前端入口和上述测试后，再以全量测试清零红名单。
