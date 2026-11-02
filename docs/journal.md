# 改造日志

## D1 — 2026-11-02

- 今天做了什么：后端 guarded 测试 64 个文件 / 628 个用例全部通过，执行耗时 47.34 秒；完成需求基线、威胁模型和辩论耦合清单，证据见 `docs/evidence/d1-backend-tests.txt`。
- 卡在哪：TTS 与 room router 耦合，D3 前必须先拆；`messages.turn_id` 已存在，D6 不能重复新增；teams 模块与 room 独立，最终产品边界仍需确认。
- 明天第一件事：确认 teams 保留边界，测启动耗时，并依据需求与威胁模型冻结 `docs/platform-contract.md`。

## D2 — 2026-11-02

- 今天做了什么：在副本建立 `v1-baseline`（`23bb2bf`）与 `feat/platform`；保留 D1 的 64 个文件 / 628 个用例 PASS 基线；5 次启动测量 p50 为 2162.824 ms，范围 2146.208—3279.485 ms，证据见 `docs/evidence/d2-startup.json`；冻结 `docs/platform-contract.md`，并将 TTS、完整多 Bot 生成块和已有 turn_id 的发现回写到 D3—D6。
- 卡在哪：沙箱平台尚未实现，最大并发沙箱只能记 `N/A`；room router 同时承载 Bot 试听所需 TTS，不能直接整段删除。D2 只新增测量脚本与文档，未修改业务代码，因此没有仅为文档变化重复全量测试。
- 明天第一件事：先将 `/tts` 与 `/tts/diag` 拆为独立 router，补 TTS / Bot 试听回归验证，再移除 room router，并分别检查 `/api/rooms` 与 `/api/hands` 的预期 404。

## D3 — 2026-11-02

- 今天做了什么：新增独立 `src/tts_api.py`，保持 `/api/tts`、`/api/tts/diag` 与桌面 edge_tts 子进程方式不变；正式应用移除 room router；新增 3 个模拟外部调用的 HTTP 回归测试。
- 验收与卡点：全量 **65 个文件 / 631 个用例 PASS，46.08 秒**，证据见 `docs/evidence/d3-backend-tests.txt`；`/api/rooms`、`/api/hands/hand-1/approve` 均为 404，TTS 和健康检查正常。旧 room 文件及其中的临时重复 TTS 代码留待 D5 删除。
- 明天第一件事：将 `utter / host_* / debater_* / bot_lean` 完整多 Bot 生成块移入 legacy，记录 7 个直接相关测试及共享 agent 测试的红名单。

## D4 — 2026-11-02

- 今天做了什么：完整多 Bot 生成块迁入 `legacy/multi_bot.py`，`src/agent.py` 只保留共享模型、提醒工具、知识库和单人 chat；7 个直接相关测试逐一实测并落盘红名单。
- 验收与卡点：直接相关 59 个用例中 13 个预期 ERROR，涉及 4 个文件；其余 3 个文件因存储或依赖注入暂时 PASS，全部列入 D5 删除面。共享 agent / Bot **71 个用例 PASS**，两个迁移模块语法检查通过。证据见 `docs/d4-red-tests.md` 与对应 evidence 文件。
- 明天第一件事：删除 room API / service / store、RoomView 与 7 个旧测试，清理 App、CSS、registry 和文档索引，跑全量测试直至红名单为 0。

## D5 — 2026-11-02（W1 检查点）

- 今天做了什么：删除 room API / service / store、RoomView 和 7 个旧测试；清理 App 导航与房间专属 CSS，保留侧栏和 TTS 共享样式；按项目命令重建 `tests/TEST_LAYERS.md`。
- 验收与卡点：全量 **58 个文件 / 572 个用例 PASS，41.17 秒**，较 D3 减少的 59 个用例与删除文件完全对应，红名单为 0；测试索引 audit PASS；`npm ci && npm run build` 前端生产构建 PASS。后端证据见 `docs/evidence/d5-backend-tests.txt`。
- 明天第一件事：只在临时旧库副本上实现并验证 run / turn / step / exec 数据模型、备份和回滚，确保 bots 表与 Bot 数据不变，不触碰真实数据目录。

## D6 — 2026-11-02

- 今天做了什么：新增 `agent_runs / agent_turns / agent_steps / agent_execs` 与 `RunStore`；迁移保留旧 `turn_id`、按 `created_at / id` 排列 step，不把旧模型发言伪造成 exec。新增显式 `--confirm-copy` 的迁移 / 回滚 CLI；建表与数据共用事务，失败自动从 SQLite 在线备份恢复。所有操作仅针对测试临时库。
- 验收与卡点：10 个新增用例覆盖归属隔离、真实工具 exec 关联、无 `stage`、事务原子性、重复迁移、旧表逐行不变和回滚；全量 **60 个文件 / 582 个用例 PASS，43.77 秒**，证据见 `docs/evidence/d6-backend-tests.txt`。同时修复前端静态目录存在时未知 `/api/hands` 被 StaticFiles 转成 405 的问题，恢复为契约要求的 404。
- 明天第一件事：阅读精简后的模型调用、提醒工具分发与聊天 HTTP 流程，再实现 SSE 和助手侧取消；不得假定 SDK 原生支持流式或线程取消。

## D7 — 2026-11-02

- 今天做了什么：`/chat` 增加可选 SSE，旧 JSON 默认行为不变；SDK 流式 delta 实时转发，取消时关闭模型流；新增活动 run 索引、取消令牌、取消 API、前端 Esc / 停止按钮、增量文本与工具状态。run / step / exec 记录 cancelled，旧聊天请求保守记为 unknown，避免误自动重试。
- 验收与卡点：真实本地 HTTP 验证普通流式、显式取消和直接断流均 PASS，取消接口返回 202，断流后后台 worker 停止且服务进程已清理；证据见 `docs/evidence/d7-live-http.txt`。测试客户端的并发流取消模拟不稳定，未作为验收依据；真实 HTTP 结果为准。前端生产构建 PASS。
- 明天第一件事：落实工具注册表和 allow / ask / deny 策略矩阵，确认 ask / deny 不产生 exec，完整审批交互留到 D13。

## D8 — 2026-11-02

- 今天做了什么：新增工具注册表与策略矩阵；流式链路中查询 / 新建提醒为 allow，修改 / 取消提醒为 ask，未知工具为 deny。策略在 exec 前判定，审批不是沙箱隔离替代品。旧 JSON 提醒行为保持原样，完整审批门留到 D13。
- 验收与卡点：策略单测、工具内取消、SSE 和归属隔离均通过；全量 **62 个文件 / 591 个用例 PASS，43.06 秒**，证据见 `docs/evidence/d7-d8-backend-tests.txt`；前端构建证据见 `docs/evidence/d7-frontend-build.txt`。
- 明天第一件事：完成一次可复跑的“发问 → 工具 → 回答”G1 证据；真实模型外部调用与录屏若需要凭据或用户操作，再明确请求确认。
