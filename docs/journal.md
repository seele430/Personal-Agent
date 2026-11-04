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
## D14 — 2026-11-03

- 今天做了什么：冻结 trajectory schema（`docs/trajectory-schema.md`：事件类型、顺序与终态规则、payload 边界、JSONL 形状），补齐 `run_started` / `run_finished` 事件，新增只读渲染层 `src/core/trajectory_view.py` 与 CLI `scripts/trace_run.py`（`trace <run_id>`、`--request-id`、`--json`、`--db`），一条对话能看到 turn / step / exec / 审批 / 全部事件。前端补上审批卡片：SSE `approval_required` 弹卡（工具名 + 参数预览 + 批准 / 拒绝），点击调用 `POST /chat/requests/{id}/approvals/{approval_id}`，并把等待审批时的流超时从 60 秒放宽到 10 分钟。
- 验收与卡点：CLI 用例 **6 个全绿**（渲染内容、JSON 序列化、文本 / JSON 输出、按 request_id 查、未知 run 退出码 2），证据见 `docs/evidence/d14-trace-cli.txt`；全量 **68 文件 / 638 用例 PASS，144.08 秒**，证据见 `docs/evidence/d14-backend-tests.txt`；前端 `tsc -b && vite build` PASS，证据见 `docs/evidence/d14-frontend-build.txt`；测试索引审计 PASS（`d14-test-audit.txt`）。加事件后修了 4 处「断言整条事件列表」的用例，改成按 `exec_id` / 事件前缀过滤，避免以后再加事件就红。
- 遗留限制：① 前端审批卡片没有浏览器端到端测试（只有 tsc / vite 构建证据），真实点击路径还没跑过；② CLI 不做跨 run 检索（`trace --limit` 之类），要按 run_id 或 request_id 查；③ trajectory 目前随 run 级联删除，没有独立保留期（等 D16—D20 的 reaper）；④ payload 上限 2 KB 只是文档约定，代码还没强制截断；⑤ `run_finished` 与 `store.finish` 不是同一事务，极端崩溃下可能出现「事件已写、状态未改」。
- 明天第一件事：D15 幂等 create（idempotency key + 指纹 + 并发只建一个沙箱），并把审批卡片的真实点击路径补成浏览器端到端用例。
## D15 — 2026-11-03

- 今天做了什么：新增平台控制面服务 `src/platform/sandbox_service.py` 与独立库 `platform.db`（`config.paths.platform_db`）。`create(idempotency_key, tier, workspace_id, ttl_seconds)` 用 SQLite 唯一约束 + `BEGIN IMMEDIATE` 同一事务实现幂等：同 key 同请求体返回同一 sandbox，同 key 不同请求体 409 `IDEMPOTENCY_CONFLICT`，非法 key 400 `INVALID_IDEMPOTENCY_KEY`，并发只允许一个创建者；`reclaim()` 幂等回收并把状态置为 reclaimed。run 级沙箱改为按 `run-<run_id>` 固定 key 创建（`agent.open_run_sandbox(run_id)`），同一个 run 重复调用不会多出第二份工作区；`destroy_run_sandbox` 走 `reclaim`，状态与磁盘一起收。沙箱根目录抽到 `src/platform/paths.py`，exec 与 create 共用同一个根。
- 验收与卡点：**10 个用例全绿**（同 key 重试 10 次只有 1 行 sandbox / 1 行幂等记录、省略 workspace 时默认值确定性规范化后指纹一致、重启后仍命中、同 key 不同 workspace 或 ttl → 409、8 线程 16 次并发只建 1 个、非法 key / ttl / tier 拒绝、回收幂等且删除工作区、run 级沙箱按 run_id 复用），证据见 `docs/evidence/d15-idempotent-create.txt`。全量 **69 文件 / 648 用例 PASS，216.01 秒**（D14 是 68 / 638、144.08 秒），证据见 `docs/evidence/d15-backend-tests.txt`；测试索引审计 PASS（`d15-test-audit.txt`）。
- 遗留限制：① 只实现服务层，**没有 HTTP `/v1/sandboxes` 入口**，D10 的 SDK 仍然只能打内存替身；② 幂等记录没有过期 / 清理（契约要求回收后保留 24 小时，等 D16 的 reaper）；③ 一次性 exec 沙箱（不传 sandbox_id 时）仍走本地随机 id，不登记到控制面；④ `expires_at` 只写不校验，TTL 语义在 D16；⑤ 没有 tombstone / 410。
- 明天第一件事：D16 reaper + TTL + 410 —— 到期回收、幂等记录保留 24 小时、已回收沙箱上的操作返回 410。
## D15.1 — 2026-11-03

- 今天做了什么：补上平台 v1 HTTP 控制面 `src/platform_api.py`（`/v1/sandboxes` 创建 / 查询 / 销毁，`/v1/sandboxes/{id}/exec`，`/v1/exec/{id}` 查询与取消），exec 走真实 tier1 子进程：202 接受 → 后台线程执行 → `GET /v1/exec/{id}` 轮询到终态；`result_code` 与契约一致（EXEC_SUCCEEDED / EXEC_TIMED_OUT / EXEC_CANCELLED / OUTPUT_LIMIT_EXCEEDED / EXEC_FAILED）。新增 `platform_execs` 表与 `ExecStore`。错误码对齐契约：404 SANDBOX_NOT_FOUND / EXEC_NOT_FOUND、410 SANDBOX_GONE、409 EXEC_NOT_CANCELLABLE。顺手修了一处 SDK 兼容问题：取消接口原先只回 `exec_id/state`，SDK 按 `ExecResult` 解析会缺字段，已改成回完整资源。
- 验收与卡点：`tests/integration/test_platform_api.py` **9 个用例全绿**（幂等 create、缺 key 400、同 key 冲突 409、exec 成功 / 超时 / 取消、非法 command 400、未知资源 404、destroy 幂等 + 410），证据见 `docs/evidence/d15-platform-api.txt`；**D10 的 Python SDK 用自带的 UrllibTransport 打真实端口**跑通全链路 —— 同 key 两次 create 同一个 sandbox、exec 拿到 `sdk-ok`、超时 `timed_out`、取消 `cancelled`、destroy 后 GET 返回 410，证据见 `docs/evidence/d15-platform-v1-sdk.json`。全量 **70 文件 / 657 用例 PASS，231.58 秒**，证据见 `docs/evidence/d15b-backend-tests.txt`。
- 遗留限制：① `/v1` **没有认证**（本机单用户控制面，只监听 127.0.0.1），D21 多租户之前不能外露；② `/v1/sandboxes/{id}/fn`、`/v1/nodes`、`/metrics` 未实现；③ exec 没有幂等（契约明确不承诺重放）；④ 没有 TTL / reaper，`expires_at` 只是记录；⑤ HTTP 测试走 ASGI TestClient，真实端口只在 verify 脚本里验过。
- 明天第一件事：D16 reaper + TTL + 410 —— 到期回收、幂等记录保留 24 小时、tombstone 语义；补 `/metrics` 或留给 D25 按计划走。
## D16 — 2026-11-03

- 今天做了什么：实现 TTL 回收与 tombstone。新增 `src/platform/reaper.py`（`sweep_once(now=...)` + 可选后台线程，间隔 `PERSONAL_AGENT_PLATFORM_REAPER_INTERVAL` 默认 60 秒）与 `src/platform/active_execs.py`（exec → sandbox → token 的注册表）：到期时**先取消该沙箱的活动 exec**（进程树由执行器清理），再把沙箱置 `reclaimed` 并写 `reclaim_reason=ttl_expired`；tombstone 与幂等记录默认保留 24 小时（`PERSONAL_AGENT_TOMBSTONE_HOURS` 可覆盖），到期在同一事务里一起清除，之后旧 key 可以重新创建。API 访问加了惰性兜底：reaper 还没跑时，访问已过期沙箱会先触发同一条回收路径再返回 410。`main.py` 的 lifespan 启动/关闭回收线程，测试环境用 `PERSONAL_AGENT_PLATFORM_REAPER=0` 关闭。
- 验收与卡点：`tests/integration/test_platform_ttl.py` **6 个用例全绿**（sweep 回收 + reason、sweep 幂等、到期取消活动 exec、惰性过期返回 410、tombstone 保留期内同 key 返回同一沙箱、超过 24 小时后 tombstone/幂等记录清除且旧 key 能建新沙箱），证据见 `docs/evidence/d16-ttl.txt`；**真实服务验证后台线程**：起 uvicorn（reaper 间隔 0.5 秒），创建 ttl=1 的沙箱后**不访问 API**、直接读 `platform.db`，确认线程把状态改成 reclaimed/ttl_expired，随后 GET 返回 410，证据见 `docs/evidence/d16-reaper.json`。全量 **71 文件 / 663 用例 PASS，248.19 秒**，证据见 `docs/evidence/d16-backend-tests.txt`。
- 遗留限制：① 回收粒度是「过期扫描间隔」，最坏会晚 60 秒才回收（惰性访问是即时兜底）；② tombstone 清理依赖同一进程的 reaper，服务停机期间不会推进；③ 沙箱状态机还没有 running/idle/failed 迁移（D20），当前只有 created/reclaimed；④ 活动 exec 表只存在进程内，多进程部署时会漏掉别的进程里的 exec（单机单进程假设）。
- 明天第一件事：D17 节点注册与心跳（`/v1/nodes`，last_heartbeat + generation），或先把 `running/idle` 状态迁移补上，为 D18 租约铺路。
## D17 — 2026-11-03

- 今天做了什么：实现节点注册与心跳（`src/platform/node_registry.py` + `/v1/nodes` 三个接口：注册、心跳、列表）。每次注册 generation 单调 +1；心跳必须匹配当前 generation，否则 409 `NODE_GENERATION_STALE`，而且**不会改写 last_heartbeat**（旧代际不能续新实例的租约）。单机部署用 `ensure_default_node()` 保证 `node_local` 这一行存在：创建沙箱时会顺带登记默认节点，节点列表因此有真实内容；沙箱响应的 `node_id` 也来自节点表，不再是硬编码常量。
- 验收与卡点：`tests/integration/test_platform_nodes.py` **9 个用例全绿**（注册分配 generation 1 / 重复注册 +1 / 心跳推进 last_heartbeat / 旧代际 409 且不改写心跳 / 未知节点 404 / 列表含 last_heartbeat / 建沙箱自动登记默认节点 / capacity 非法 400 / 仓储层异常类型），证据见 `docs/evidence/d17-nodes.txt`；全量 **72 文件 / 672 用例 PASS，187.42 秒**（D16 基线 71 / 663，248.19 秒）。
- 遗留限制：① **租约过期判定没做**（连续 3 个周期无心跳 → not_ready），按计划属于 D18；② 没有 cordon / drain（D19）；③ generation 只保留当前值，历史代际不留痕，排障时看不到「谁在什么时候注册了第几代」；④ 节点容量字段只是记录，还没有调度（D20）；⑤ `/v1/nodes` 同样没有认证。
- 明天第一件事：D18 租约 + 续约 + 过期判定 —— 停心跳 3 个周期后 `not_ready`，旧 generation 的延迟心跳继续 409。
## D18 — 2026-11-03

- 今天做了什么：实现节点租约判定。`agent_nodes` 增加 `heartbeat_interval_seconds`（旧库补列，幂等），节点状态改为**由 `last_heartbeat` 推导**：超过 `间隔 × 3` 即 `not_ready`，响应同时给出 `lease_seconds`、`lease_expires_at`、`seconds_since_heartbeat`、`expired`。注册时可指定心跳间隔（1—300 秒），重复注册 generation +1 并刷新租约；有效 generation 的心跳把状态刷回 `ready`，旧 generation 仍然是 409 且不改写心跳时间。状态推导在读路径完成，不依赖后台任务刚好跑过。
- 验收与卡点：`tests/integration/test_platform_lease.py` **7 个用例全绿**（间隔 × 3 的租约参数、29s 仍 ready / 31s not_ready、心跳续约、旧代际不能复活、非法间隔 400、API 真实时钟 1 秒周期等 3.4 秒后 not_ready、心跳恢复 ready），证据见 `docs/evidence/d18-lease.txt`；全量 **73 文件 / 679 用例 PASS，298.91 秒**（D17 是 72 / 672、187.42 秒；这次慢在整机负载，用例数与文件数都是净增），证据见 `docs/evidence\d18-backend-tests.txt`。
- 遗留限制：① 过期只影响「状态展示」，还没有调度器消费这个状态（D20/D23）；② 没有 cordon / drain（D19）；③ 节点 generation 只保留当前值，历史代际不留痕；④ `/v1/nodes` 仍无认证；⑤ 租约判定用本机时钟，没有做时钟漂移处理。
- 明天第一件事：D19 cordon / drain —— 摘流不杀存量、drain 期间不接新活、drain 完成才允许 remove。
## D19 — 2026-11-04

- 今天做了什么：实现 cordon / drain / remove。`agent_nodes` 增加 `cordoned_at` / `draining_at` / `removed_at`（旧库补列），`agent_sandboxes` 增加 `node_id`（建沙箱时写入默认节点，旧库补列）；节点视图新增 `cordoned` / `draining` / `removed` / `accepting` / `active_sandboxes`。`POST /v1/sandboxes` 在默认节点 `accepting=false` 时返回 429 `NO_READY_NODE`；`remove` 要求「已 drain + 该节点未回收沙箱数为 0」，否则 409 `INVALID_STATE_TRANSITION`（`retryable=true`）；已移除节点拒绝心跳和再次 cordon / drain。
- 验收与卡点：`tests/integration/test_platform_node_ops.py` **7 个用例全绿**（cordon 阻断新沙箱但租约仍 ready、cordon 幂等、cordon 不杀运行中的 exec（真实子进程跑到 succeeded）、remove 必须先 drain、drain 后仍有沙箱时 remove 409 且可重试、回收后 remove 200、已移除节点拒绝心跳/运维、`active_sandboxes` 计数），证据见 `docs/evidence/d19-node-ops.txt`；全量 **74 文件 / 686 用例 PASS，187.92 秒**（D18 基线 73 / 679，298.91 秒 —— 上一轮慢在整机负载，这次回到正常水平），证据见 `docs/evidence/d19-backend-tests.txt`。
- 遗留限制：① 还没有 least-loaded 调度与容量原子占用（D20/D21），单机只有一个默认节点；② 没有 uncordon，摘流只能靠重新注册；③ remove 不做进程清理，只保证「无未回收沙箱」；④ `capacity` 仍未参与准入判断；⑤ `agent_sandboxes.node_id` 只有新行有值，D18 之前的历史行是 NULL，计数时不计入（回填留给 D20 一起做）。
- 明天第一件事：D20 沙箱状态机（created → running ↔ idle → reclaimed/failed），非法迁移要有测试，并顺手回填历史行的 node_id。
## D20 — 2026-11-04

- 今天做了什么：实现沙箱状态机。`agent_sandboxes` 的 CHECK 从 created/reclaimed 两态重建为五态（created / running / idle / failed / reclaimed），旧库原地迁移：建新表 → 拷贝（缺失列用 NULL / `node_local` 兜底）→ 删旧表 → 改名 —— 顺序是故意的，SQLite 在 rename 时会改写其它表里指向被改名表的 REFERENCES，先改名会把 idempotency 的外键指到 legacy 表上。新增 `SandboxService.transition()` 与合法迁移表：`begin_exec`（created / idle → running）、`end_exec`（活动 exec 归零时 running → idle）、`mark_failed`；`reclaim` 也改走同一张合法表。`platform_execs` 新增 `count_active()`，API 的 exec 创建/结束接到状态机上，failed 沙箱拒绝新 exec（409）。
- 验收与卡点：`tests/integration/test_sandbox_state.py` **8 个用例全绿**（created→running→idle→reclaimed 全链、failed 只能回收、非法迁移 409、同状态幂等、reclaimed 拒绝 begin_exec、并发 exec 保持 running、API 中 exec 让沙箱 running 再回 idle、failed 拒绝 exec、旧库重建 + node_id 回填 + 可重复迁移），证据见 `docs/evidence/d20-sandbox-state.txt`；平台相关 5 个文件一起跑 40 个用例也全绿。全量 **75 文件 / 694 用例 PASS，212.02 秒**，证据见 `docs/evidence/d20-backend-tests.txt`。
- 遗留限制：① 还没有调度器消费 running / idle（D23）；② 没有 per-tenant 配额与容量原子占用（D21）；③ `failed` 目前只能通过服务层手动标记，执行器崩溃不会自动置 failed（需要 D22 启动对账）；④ 迁移只覆盖 agent_sandboxes，platform_execs 没有历史包袱；⑤ `list_expired` 现在也覆盖 running / idle 沙箱，到期回收会先取消活动 exec（D16 已有测试覆盖）。
- 明天第一件事：D21 每租户并发配额 —— 超额返回 429 `CAPACITY_EXCEEDED`，回收后释放名额。
## D21 — 2026-11-04

- 今天做了什么：实现每租户并发配额与节点容量准入。`agent_sandboxes` 增加 `tenant_id`（旧库补列，默认 `default`；`/v1` 可用 `X-Tenant-Id` 头覆盖）；`SandboxService.create` 在同一事务里先查「该租户未回收沙箱数 < 配额」再查「该节点未回收沙箱数 < capacity」，超限抛 `QuotaExceeded`（429 `CAPACITY_EXCEEDED`，`retryable=true`）并回滚；配额来自 `PERSONAL_AGENT_SANDBOX_QUOTA`（默认 8，上限 1000）。幂等重放先命中 key，不占新名额；回收后按 `state != 'reclaimed'` 自动释放。新增 `count_for_tenant()`。
- 验收与卡点：`tests/integration/test_sandbox_quota.py` **6 个用例全绿**（配额 2 时第三个 429 且可重试、回收后放行、幂等重放不占名额、节点 capacity=1 时第二个 429、非法配额 env 回退默认值、10 线程并发只成功 2 个、两个租户互不影响），证据见 `docs/evidence/d21-quota.txt`；全量 **76 文件 / 700 用例 PASS，199.68 秒**，证据见 `docs/evidence/d21-backend-tests.txt`。
- 遗留限制：① `/v1` 没有认证，`X-Tenant-Id` 由调用方自报，**不能当安全边界**（D21 只做配额机制）；② 没有 least-loaded 调度，单机只有一个默认节点；③ 配额算的是「未回收沙箱数」而不是「活动 exec 数」，两者在有 idle 沙箱时会不同；④ 只有租户级配额，没有项目级二级配额；⑤ 并发用例是 8 线程的功能验证，不是压测。
- 明天第一件事：D22 启动对账 —— 重启后清理孤儿沙箱 / exec，并让执行器崩溃能落到 `failed`。
## D22 — 2026-11-04

- 今天做了什么：实现启动对账（`src/platform/reconcile.py`，在 lifespan 里、reaper 之前跑）。重启后：① `queued / running` 的 exec 落 `failed / INTERRUPTED_BY_RESTART`（**不伪造 cancelled**）；② `running` 但没有活动 exec 的沙箱回到 `idle`；③ 工作区缺失的沙箱落 `failed / workspace_missing`（新增 `fail_reason` 列）；④ 沙箱根目录下 `sbx_` 命名但没有数据库记录的目录会被删除，其它目录一律不碰。对账幂等，并跳过本进程内确实在跑的 exec（`active_execs` 注册表）。
- 验收与卡点：`tests/integration/test_platform_reconcile.py` **7 个用例全绿**（中断 exec → failed 且沙箱回 idle、活动 exec 不被误伤、running 无 exec → idle、缺工作区 → failed、已回收不受影响、孤儿目录删除但陌生目录保留、对账幂等），证据见 `docs/evidence/d22-reconcile-tests.txt`；**真实重启验证**：起服务建沙箱 → 杀掉 → 删工作区 + 插一条 running exec + 放一个孤儿目录 → 重启 → 直接读库确认 `failed / workspace_missing`、`INTERRUPTED_BY_RESTART`、孤儿目录消失，证据见 `docs/evidence/d22-reconcile.json`。全量 **77 文件 / 707 用例 PASS，203.84 秒**，证据见 `docs/evidence/d22-backend-tests.txt`。
- 遗留限制：① 对账按「单进程」假设，多进程部署时别的进程正在跑的 exec 会被误判（当前是单机单进程）；② 平台重启但 runner 进程还活着的情况发现不了 —— 需要把 runner PID 持久化才能精确回收；③ 只在启动时对账一次，没有定期对账；④ 孤儿目录按 `sbx_` 命名判断，若有人手工建同名目录会被当成孤儿（在平台根目录下属于本平台领地）。
- 明天第一件事：D23 G2 全链路 —— 助手 → 平台 → 沙箱 → 轨迹，准备 3 条 demo 命令与演示口径。