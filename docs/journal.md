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
