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

## D9 — 2026-11-02（G1 验收）

- 今天做了什么：新增可复跑的本地确定性链路与真实模型 HTTP 验收脚本；使用 DeepSeek Flash（API 模型名 `deepseek-flash`）完成真实“发问 → 工具 → 回答”，并修复工具完成状态在前端停留过短的问题。新增真实浏览器录屏脚本，自动登录临时 member 账号、创建提醒并检查提醒页面。
- 验收与卡点：本地链路 PASS，明确未调用外部模型，证据见 `docs/evidence/d9-local-tool-chain.txt`；真实外部模型调用 PASS，事件顺序为 `run_started → tool_started → tool_finished → text_delta … → done`，run / exec 均为 succeeded，证据见 `docs/evidence/d9-live-model.txt`。最终录屏的浏览器操作窗口为 60.509 秒，视频为 `docs/evidence/d9-g1-demo.webm`，工具完成、回答和提醒关键帧分别为 `d9-frame-tool.png`、`d9-frame-answer.png`、`d9-frame-reminder.png`；仅在临时数据库新增 1 条 `in_app` 提醒，未发送外部通知，临时服务已关闭。全量 **62 个文件 / 591 个用例 PASS，42.66 秒**，证据见 `docs/evidence/d9-backend-tests.txt`；前端生产构建 PASS，证据见 `docs/evidence/d9-frontend-build.txt`。
- 明天第一件事：开始 D10，封装 Python SDK，并建立 v1 兼容性 fixture；保持真实模型验收与确定性测试证据分离。

## D10 — 2026-11-02（W2 检查点）

- 今天做了什么：新增可独立构建的 `sdk/python` 包，封装 create / exec / fn / get_exec / cancel / destroy / metrics；支持执行超时参数、默认轮询终态、主动取消、等待超时自动取消和仅停止等待选项，HTTP 状态与稳定错误码映射为 SDK 异常。新增 `sdk/fixtures/v1/contracts.json`，以 14 组语言中立场景冻结成功、执行超时、取消、等待超时和 400 / 404 / 409 / 410 / 429 错误；所有契约测试使用内存传输替身，未宣称平台 HTTP 服务已接通。
- 验收与卡点：SDK 定向 **8 个测试方法 / 14 组 fixture PASS**，证据见 `docs/evidence/d10-sdk-contract-tests.txt`；SDK wheel 构建 PASS；测试索引审计 PASS，证据见 `docs/evidence/d10-test-audit.txt`；全量 **63 个文件 / 599 个用例 PASS，61.95 秒**，证据见 `docs/evidence/d10-backend-tests.txt`。显式空幂等键会原样发送并由服务端错误语义处理，非法等待时间在创建 exec 前拒绝。
- 明天第一件事：开始 D11，将 exec 接入 tier1 process 沙箱并接通全链路取消；搜索并删除宿主直跑路径，验证取消后进程树无残留，同时明确 tier1 不是恶意代码安全边界。

## D11 — 2026-11-03

- 今天做了什么：把 allow 工具的执行从宿主进程搬到 tier1 平台执行层。新增 `src/platform/process_tree.py`（Windows Job Object：`KILL_ON_JOB_CLOSE` + `TerminateJobObject`，只有 `ActiveProcesses == 0` 之后才允许落终态）、`src/platform/tier1_runner.py`（stdin 放行闸门、独立工作区、白名单环境、`--data-dir` 显式授权）、`src/platform/exec_service.py`（queued→running→终态、timeout、stdout+stderr 合计一个输出上限、取消回调、清树后才返回）。`agent_stream` 与旧 JSON 聊天都不再在宿主调用 handler；`ToolDefinition` 增加 `execution` 描述执行位置；`RunStore` 增加 `start_exec()`（唯一合法的 queued→running 迁移）、终态不可覆盖约束与最小 `agent_trajectory_events` 表（`schema_version=1`）。
- 验收与卡点：真实进程测试 11 个用例（父＋子进程被清、超时、输出上限、环境清洗、工具只在子进程执行、ask 不产生 exec、平台超时终态、取消事件顺序）；真实 HTTP/SSE 验收两条路径（显式 cancel 返回 202、直接断流）都让 exec 落 `cancelled / EXEC_CANCELLED`，探针父子 PID 均不再存活，无关进程存活；trajectory 顺序为 `exec_started → exec_cancel_requested → exec_finished(cancelled)`，三条事件共享 run / step / exec / sandbox。证据：`docs/evidence/d11-runner-tests.txt`、`d11-live-cancel.json`、`d11-live-cancel.txt`、`d11-host-exec-search.txt`。全量 **65 个文件 / 610 个用例 PASS，118.66 秒**（基线 63 / 599，86.47 秒；变慢主要来自每个工具调用新增约 1.5 秒 runner 冷启动），证据见 `docs/evidence/d11-backend-tests.txt`；测试索引审计 PASS（`d11-test-audit.txt`）；D10 v1 fixture 契约复跑 8 个测试 PASS（`d11-sdk-contract.txt`）。
- 遗留限制：① 每次工具调用都新起一个 Python runner（`import src.agent` 约 1.44 秒），D12 之后可考虑按沙箱复用；② tier1 没有网络隔离和文件系统 jail，提醒工具通过平台显式授予的 `--data-dir` 打开数据库，这是授权不是隔离；③ 宿主侧 patch 无法作用于子进程，`IN_APP_DELIVERY_ENABLED` 这类进程内开关的用例改为直接验证 handler 规则；④ 旧 JSON 聊天在 D13 之前仍不接审批门（与 D8 前行为一致），但执行位置已经在沙箱里；⑤ 完整 trajectory schema / CLI、HTTP 控制面、幂等 create、节点租约与 TTL 仍属于 D14—D20。
- 明天第一件事：开始 D12，补齐 read / write / ls / shell / python / http 六个工具，每个工具一次受控 exec，并补 `..` / 绝对路径 / 符号链接的负向测试。
## D12 — 2026-11-03

- 今天做了什么：新增 `src/tools/sandbox_tools.py`，六个工具（read_file / write_file / list_dir / run_shell / run_python / http_get）全部作为 tier1 handler 在 runner 子进程里执行。路径规则改成「先解析 real path，再判断是否仍在工作区」，拒绝 `..`、绝对路径、盘符路径与符号链接逃逸，不用字符串前缀判断。`Tier1Executor` 增加 `open_sandbox()` 与 `sandbox_id` 复用参数；`run_chat_stream` 为每次 run 建一个共享工作区并在 finally 回收，所以同一次对话的 write → read 看到同一批文件，文件不跨 run 残留。
- 验收与卡点：`tests/integration/test_sandbox_tools.py` 11 个用例（write→read 同沙箱、列表、读取截断、写入超限、5 组路径逃逸、目录联接逃逸、shell 退出码、python 执行、执行输出边读边截断、http 失败归类、助手链路恰好一次 exec + trajectory），**11 passed / 0 skipped**（符号链接逃逸改用目录联接 junction 覆盖，普通账户即可创建），证据见 `docs/evidence/d12-sandbox-tools.txt`。全量 **66 文件 / 621 用例 PASS，191.45 秒**（D11 是 65 / 610、118.66 秒；增量来自每个工具一次真实子进程冷启动），证据见 `docs/evidence/d12-backend-tests.txt`；测试索引审计 PASS（`d12-test-audit.txt`）。
- 遗留限制：① 目录联接（junction）逃逸已覆盖，但真正的 NTFS 符号链接需要额外权限，本机没有单独验证；② write / shell / python / http 仍是 ask，模型看不到、也无法通过对话触发，等 D13 审批门；③ 每次工具调用仍要新起 runner（约 1.5 秒），六工具串联会明显变慢，D13 之后应评估 runner 复用；④ 工具参数上限从 4096 提到 32768 才能写文件，单次请求体积上限因此放大，需要在 D13 / D21 一并评估；⑤ `run_shell` / `run_python` 的 stdout / stderr 现在是边读边截断（超限杀子进程），但退出码会因此变成被杀的码值。
- 明天第一件事：D13 审批门接入对话流 —— 给 ask 工具补审批状态机、拒绝路径测试和前端交互，再决定哪些沙箱工具对模型开放。设计草案见 `docs/d13-approval-plan.md`。
## D13 — 2026-11-03

- 今天做了什么：实现审批门（设计见 `docs/d13-approval-plan.md`）。新增 `agent_approvals` 表与 `src/core/approvals.py`（参数摘要、预览、50ms 轮询等待、按 request 决定）；`agent_stream` 的 ask 分支改成「建审批 → SSE 发 `approval_required`（带 `approval_id` 与参数预览）→ 等待决定 → 批准之后才建 exec 并执行」，run 结束时把残留 pending 统一过期；新增 `GET /chat/requests/{id}/approvals` 与 `POST .../approvals/{approval_id}`（202 接受 / 404 非本人 / 409 已决定）；trajectory 新增 `tool_approval_requested` 与 `tool_approval_decided`。
- 验收与卡点：`tests/integration/test_approval_gate.py` **11 个用例全绿**（批准前无 exec、批准后恰好一次 exec 且事件顺序 requested → decided → exec_started → exec_finished、拒绝 / 超时 / 取消都不产生 exec、重复决定 409、非本人 404、参数摘要被换掉拒绝执行），证据见 `docs/evidence/d13-approval-gate.txt`；全量 **67 文件 / 632 用例 PASS，167.25 秒**，证据见 `docs/evidence/d13-backend-tests.txt`；测试索引审计 PASS（`d13-test-audit.txt`）。同时修掉一个 D12 遗留：旧 `test_ask_tool_never_creates_exec` 断言的是「ask 直接拒绝」，D13 后会让用例白等 300 秒审批超时，已改成「进入审批门 → 拒绝 → 无 exec」。
- 遗留限制：① 前端没有审批 UI，只有 SSE 事件（`approval_required` / `approval_decided`），现在只能靠接口批准；② 危险工具仍不暴露给模型（write / shell / python / http 全是 ask，schema 也不下发）；③ 审批超时默认 300 秒，过期后 run 继续走，模型只会收到「审批超时」；④ 审批没有独立审计导出，状态只在 SQLite 里；⑤ HTTP 用例把 legacy 的 `_read` 换成了替身，路由只验证审批段；⑥ 审批等待是轮询，长时间挂起会有 20 次/秒的 SQLite 读，量大时需要换成事件通知。
- 明天第一件事：D14 轨迹落库 + CLI `trace <run_id>`，把审批事件、exec 事件和取消事件串成一条可读轨迹；顺手补前端审批交互。