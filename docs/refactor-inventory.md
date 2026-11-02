# 多 Bot 辩论改造清单与 D1 基线

> 日期：2026-11-02
> 范围：D1 体检；未修改业务代码。
> 行数口径：`wc -l`；调用点按当前工作区副本实际检索结果记录。

## 1. 后端测试基线

执行命令：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\test.ps1
```

`test.ps1` 默认运行 `backend`，通过 guarded runner 使用临时数据目录、替换真实模型凭据并启用 SQLite / socket guard。

| 指标 | D1 实测值 |
| --- | ---: |
| 测试文件 | 64 |
| 总用例 | **628** |
| unit | 91 |
| integration | 392 |
| api | 134 |
| legacy | 11 |
| 测试执行耗时 | **47.34 s** |
| 结果 | **PASS** |

证据：`docs/evidence/d1-backend-tests.txt`，开头第 18—29 行为文件 / 用例分布，末尾记录 `PASS: backend, 628 tests`、`Duration: 47.34s` 和 `SUCCESS: backend`。

README 仍写“56 个测试文件 / 617 个用例”，已过期；D1 后统一使用本次实测的 **64 / 628**，不沿用旧数字。

D2 再测启动耗时；沙箱尚未建立，最大并发沙箱当前记 `N/A`，不补造基线。

## 2. 核心代码耦合面

| 文件 | 行数 | 当前职责与直接调用点 | 处置建议 |
| --- | ---: | --- | --- |
| `src/main.py` | 216 | 第 21 行导入 `room_router`，第 137 行注册 | 修改；先处理 TTS 依赖，再移除 room router |
| `src/room_api.py` | 1014 | 同时包含 TTS、房间、举手、投票和总结接口 | 拆分后删除房间部分；**不能整文件直接删** |
| `src/core/room_service.py` | 911 | 辩论编排；调用 `agent.utter / host_* / debater_* / bot_lean` | 删除或移入 legacy，保留可复用设计记录 |
| `src/core/room_store.py` | 1232 | rooms / messages / hands / votes / scores 与 stage 状态机 | 删除业务实现前先做数据迁移决策 |
| `src/agent.py` | 1653 | 多 Bot 生成块集中在约 572—1341 行；工具与单人 chat 从 1342 行继续 | 精准移除辩论块，保留共享模型调用、工具、额度和单人 chat |
| `frontend/src/RoomView.tsx` | 1919 | 房间与辩论 UI，调用 `/api/rooms/**` | 删除；保留共享组件 / 样式的复用评估 |
| `frontend/src/App.tsx` | 2352 | 第 11、432、1472—1475、2064—2065 行接入 RoomView | 修改 import、view union、导航和渲染分支 |
| `frontend/src/index.css` | 2140 | 含大量 `.room-*`、`.stage-*` 选择器 | 按选择器清理，不按行段盲删 |
| `desktop/run_desktop.py` | — | 注释明确引用 `src/room_api.py` 的 edge_tts 子进程方式 | TTS 拆分时保持桌面兼容并更新注释 |

### 2.1 `agent.py` 不能只处理 `debater_*`

多 Bot 相关函数包括：

- `utter`：572 行；当前在 `room_service.py:49、152、441、773` 使用。
- `host_summary`：662 行；在 `room_service.py:858` 使用。
- `_stance_lines` / `_side_of`：723 / 746 行。
- `host_opening`：756 行；在 `room_service.py:240` 使用。
- `debater_opening`：847 行；在 `room_service.py:241、352` 使用。
- `_debater_identity`：945 行。
- `debater_rebuttal` / `debater_question` / `debater_closing`：976 / 1034 / 1087 行；在 `room_service.py:438-440` 使用。
- `host_vote`：1144 行；在 `room_service.py:442` 使用。
- `bot_lean`：1193 行；在 `room_api.py:689` 使用。
- `debater_free_turn`：1248 行；在 `room_service.py:646` 使用。

因此 D4 若只移动 `debater_*`，会留下 `utter / host_* / bot_lean` 及其测试。建议按“多 Bot 生成块”处理，同时保护 `build_system_prompt`、`_completion`、用量追踪、工具执行与 `chat()` 等共享能力。

### 2.2 TTS 是 room router 里的隐藏共享依赖

- `src/room_api.py:56` 注册 `/tts/diag`，第 232 行注册 `/tts`。
- `frontend/src/lib/tts.ts:82` 请求 `/api/tts`。
- `frontend/src/BotsView.tsx:8` 仍导入 `previewVoice / prewarmVoices`，Bot 管理页不依赖 RoomView 也会使用 TTS。
- `desktop/run_desktop.py:93-106` 对 edge_tts 子进程有打包兼容逻辑。

**结论：D3 直接移除 `room_router` 会同时让 `/api/tts`、`/api/tts/diag` 失效，不只影响 `/api/rooms`。**

推荐前置顺序：先把 TTS 拆成独立 router 并补回归验证，再移除 room router。若坚持先摘 router，必须把 TTS 暂时失效写入红名单，不能记为“其他接口不回归”。

此外，room router 还包含 `/hands/{hand_id}/approve|reject`（`room_api.py:847、863`），D3 验收不能只检查 `/api/rooms`。

### 2.3 数据库与 D6 的实际耦合

- `room_store.py:8` 已说明消息按 `turn_id` 归组。
- `room_store.py:154-170` 的 `messages` 已包含 `turn_id` 和 `stage`，因此 D6 **不是从零新增 turn_id**。
- `rooms` 与 `messages` 都有 `stage`，需分别处理，不能只改消息字段。
- `RoomStore` 默认使用 `paths.bots_db`，并确保 `BotStore` 存在，见 `room_store.py:56-65`。
- `room_bots.bot_id` / `messages.bot_id` 外键引用 `bots.id`，见 `room_store.py:10-11、124-170`。
- `BotStore` 与房间数据共用 `bots.db`，不能为了删房间功能直接删除数据库或 bots 表。

D6 应改成：保留现有 turn_id，新增 assistant 所需的 step / 顺序语义，剥离 stage 的辩论含义，并在副本上验证备份与回滚。优先评估新 runs / steps / exec 数据模型，避免把旧 messages 表原地改成含义混杂的表。

## 3. 前端调用面

`RoomView.tsx` 完整覆盖：

- 房间列表 / 创建 / 归档 / 删除；
- 房间 Bot、消息、开场、阶段推进、自由回合；
- 投票、评分、倾向、举手、总结和决策；
- TTS 播放。

关键入口：

- `App.tsx:11`：导入 RoomView。
- `App.tsx:432`：`view` union 中的 `rooms`。
- `App.tsx:1472-1475`：导航按钮。
- `App.tsx:2064-2065`：RoomView 渲染。
- `RoomView.tsx:213` 起：`/api/rooms` 系列请求。

`frontend/src/lib/tts.ts` 和 `BotsView.tsx` 属于保留面，不能随 RoomView 一起删除。

## 4. 直接相关测试与注册表

| 文件 | 行数 | 处置 |
| --- | ---: | --- |
| `tests/test_utter.py` | 104 | 随多 Bot `utter` 去留处理 |
| `tests/test_room_api.py` | 218 | 房间接口删除后移除；TTS 测试另建，不依赖 room router |
| `tests/test_room_expand.py` | 186 | 房间扩展 / 举手链路 |
| `tests/test_room_hands.py` | 129 | 举手链路 |
| `tests/test_room_service.py` | 496 | 辩论编排 |
| `tests/test_room_store.py` | 497 | 房间存储与 stage |
| `tests/test_room_summary.py` | 116 | host 总结链路 |
| **合计** | **1746** | 7 个直接相关测试文件 |

注册与生成文档同步点：

- `tests/registry.py:588-648` 登记上述 room / utter 测试。
- `tests/TEST_LAYERS.md:30、72-76、98` 包含对应索引。
- `TEST_LAYERS.md` 是由 runner 生成 / 校验的文档，删除测试后应按项目机制更新，不能只手删表格行。

`tests/test_agent_bounds.py`、`test_agent_call_count.py`、`test_agent_usage*.py` 与 `test_bot*` 未命中本轮直接 room / debater 调用面，按保留面处理；每次删除后仍须跑全量测试，不能仅凭名称判断安全。

## 5. 必须保留的共享模块

- `src/api.py` / `src/chat_secure_api.py`：单人聊天、request_id 幂等、归属、并发和预算保护。
- `src/agent.py` 中共享模型调用、输入校验、用量追踪、提醒工具、知识库和 `chat()`。
- `src/core/bot_store.py`、`src/bot_api.py`：Bot 管理与 bots 表。
- `src/core/trace_store.py`、`trace_context.py`：请求与 span 可观测基础。
- `frontend/src/lib/tts.ts`、`BotsView.tsx`：保留的语音能力。
- `scripts/run_guarded_tests.py`：测试隔离机制；它不是 OS 沙箱，但应继续用于回归。

## 6. 独立但不属于本次 room 删除面的模块

`src/teams_api.py` 与 `src/core/team_store.py` 是独立的团队任务分配链路，文件注释明确说明与聊天室独立，本轮检索未发现其依赖 room / debater。

它们当前也不属于 D3—D5 的删除清单，但“一个 Agent 助手”的最终产品边界需要另行确认：保留为独立实验模块，或在后续单独移除。D1 不把它们混入 room 改造，避免无意扩大删除范围。

`scripts/run_reminder_eval.py:158` 只有“后续对比辩论 / 批评者策略”的注释，不构成运行依赖，可后续清理文案。

## 7. 建议的 D3—D5 实施顺序

1. **先拆 TTS**：独立 router、保留 `/tts` 与 `/tts/diag` 路径、验证 Bot 试听与桌面 edge_tts 行为。
2. **再摘 room router**：同时检查 `/rooms`、`/hands` 与无关接口，确认只有预期接口失效。
3. **处理完整多 Bot 生成块**：不只移动 `debater_*`；记录 7 个直接相关测试和共享 agent 测试的红名单。
4. **删除 room service / store / RoomView**：更新 App、CSS、registry 和生成的测试索引。
5. **再处理 D6 数据模型**：保留 turn_id，设计 step / exec，旧库只在副本上迁移并验证回滚。

以上顺序只消除已确认的耦合风险，不代表在 D1 提前执行代码修改。

## 8. D1 结论

当前后端基线健康：**628 个用例全部通过**。辩论功能不是孤立目录，而是横跨 API、模型生成、SQLite、前端、TTS、桌面兼容和测试注册表。

最大的三个返工风险是：

1. 直接移除 room router 导致仍保留的 TTS 同步失效；
2. 只删 `debater_*`，遗漏 `utter / host_* / bot_lean` 和第 7 个 `test_utter.py`；
3. D6 重复新增已有 turn_id，或在共享 `bots.db` 上做破坏性迁移。

后续每天删除后都以同一命令重跑，并记录实际用例变化原因；测试数量只作信息，不以“不得少于 628”为目标。
