# Personal Agent — D11 实施交接

> 交接日期：2026-11-02  
> 当前阶段：D10 已完成，D11 仅完成代码阅读与耦合分析，**尚未开始业务实现**。  
> 本文件是交接说明，不是 D11 验收证据。

## 1. 工作目录与版本

- 工作项目副本：`C:\Users\123\Documents\kimi\tasks\2026-11-02\10-57-59-1d503415\Personal-Agent`
- 修订计划：`C:\Users\123\Documents\kimi\tasks\2026-11-02\10-57-59-1d503415\PLAN-40D-revised.md`
- 不要修改原项目：`C:\Users\123\Personal Agent`
- 分支：`feat/platform`
- 当前提交：`36e29a8 d10: 封装 Python SDK 并冻结 v1 兼容契约`
- 基线标签：`v1-baseline`
- 写本交接前工作区干净；本文件生成后应只有 `HANDOFF_D11.md` 是新增未跟踪文件。

开始工作前执行：

```bash
cd "C:\Users\123\Documents\kimi\tasks\2026-11-02\10-57-59-1d503415\Personal-Agent"
git status --short --branch
git log -1 --format='%h %s'
```

## 2. 已完成进度

- D9／G1：真实 `deepseek-flash` “发问 → `create_reminder` → 回答”通过，真实浏览器录屏及关键帧在 `docs/evidence/`。
- D10：Python SDK 与 v1 fixture 已完成：
  - SDK：`sdk/python/personal_agent_platform/`
  - 共享 fixture：`sdk/fixtures/v1/contracts.json`
  - 契约测试：`tests/unit/test_platform_sdk.py`
  - 14 组场景覆盖 create／exec／fn／get_exec／cancel／destroy／metrics、超时、取消和典型错误。
- D10 最终全量回归：**63 个测试文件 / 599 个用例 PASS，61.95 秒**。
- D10 证据：
  - `docs/evidence/d10-sdk-contract-tests.txt`
  - `docs/evidence/d10-test-audit.txt`
  - `docs/evidence/d10-backend-tests.txt`

D10 只证明 SDK 与 fixture 契约，**平台 HTTP 服务和真实 tier1 runner 尚未实现**。

## 3. D11 目标与硬验收

计划原文对应 `PLAN-40D-revised.md` 的 D11：

> exec 全部走沙箱（tier1 process）+ 接通全链路取消。

必须同时满足：

- [ ] 实际工具 exec 不再由助手／API 进程直接执行；搜索证据能解释所有保留的 `subprocess`／`Popen` 路径。
- [ ] exec 使用独立工作区和清洗后的环境，支持 timeout、输出上限与稳定终态。
- [ ] 客户端断开／Esc 后，取消从助手传播到平台 exec 和 runner。
- [ ] 平台 exec 最终为 `cancelled`，且只有在 runner 进程树清理完成后才能落该终态。
- [ ] 用真实父进程＋子进程测试证明没有残留进程，不能只杀直接父进程。
- [ ] trajectory 中存在可关联 run／step／exec 的取消事件。
- [ ] 文档明确 tier1 只用于受信任代码或开发演示，不是恶意代码安全隔离。
- [ ] 全量测试、测试索引审计和 D10 v1 fixture 契约仍通过。

**仅新增一个未被实际工具调用的 runner，不算完成 D11。**

## 4. 当前实现与关键耦合点

### 4.1 断流和主动取消入口已经存在

`src/chat_secure_api.py`：

- `cancel_chat_request()` 通过 `active_runs.cancel()` 取消活动 run。
- `_stream_chat_events()` 的 `watch_disconnect()` 在客户端断流时调用 `cancel_token.cancel()`。
- SSE generator 的 `finally` 也会取消尚未完成的 worker，并等待 worker／disconnect task 收尾。

这部分可复用，不需要另建一套取消入口。

### 4.2 工具仍在 API 进程中直接执行

`src/agent_stream.py` 当前顺序：

1. 创建 tool step。
2. allow 工具先由 `RunStore.record_exec()` 写入一条 running exec。
3. 发出 `tool_started`。
4. 直接调用 `agent.TOOL_REGISTRY.execute(tool_call, cancel_token=...)`。
5. 根据返回或异常更新 step／exec。

关键问题：这里没有平台控制面、沙箱或 runner。取消时目前只是把数据库 exec 标为 cancelled，没有 OS 进程树可清理。

### 4.3 当前工具 handler 直接读写提醒数据库

`src/agent.py` 的 `_execute_reminder_tool()` 直接处理：

- `list_reminders`：allow
- `create_reminder`：allow
- `update_reminder`：ask
- `cancel_reminder`：ask

这些 handler 依赖 `src.core.reminder_identity` 的认证上下文。若把工具执行移入子进程，必须显式、可审计地传递必要的 user／request 归属上下文，并保留原有归属校验；不能退化成默认 admin，也不能复制宿主完整环境或凭据。

`src/core/tool_registry.py` 已在 handler 前执行 allow／ask／deny；ask／deny 不应产生 exec。审批仍不是隔离边界。

**实施前需特别核对范围：** 计划总目标是每次工具调用都经过平台。若准备把提醒数据库工具保留为宿主直跑、只让未来 shell／python 走 runner，应先向 Jessica 确认例外，不能自行缩小“全部走沙箱”的验收口径。

### 4.4 取消令牌可直接连接 runner

`src/core/run_control.py` 的 `CancellationToken` 支持：

- `check()`
- `register_canceller(callback)`
- 注销 callback
- `cancel()` 触发所有活动 callback

推荐让平台 exec 注册取消回调，并在执行结束／异常后注销。需要处理“注册前已经取消”和“取消与正常结束竞争”。

### 4.5 RunStore 可复用，但不要误用状态接口

`src/core/run_store.py` 已支持：

- exec 状态：queued／running／succeeded／failed／cancelled／timed_out
- run／turn／step／exec 关联
- sandbox_id 字段
- result／error_code／started_at／finished_at

注意：`update_exec()` 当前会同时写 `finished_at`，更像终态更新接口。若需要 queued → running 的中间迁移，不要直接复用它制造“running 却已 finished”的记录；应增加明确的中间状态更新方法，并保持合法状态迁移约束。

### 4.6 现有取消测试不是进程清理证据

`tests/test_agent_stream.py` 的 `test_cancel_enters_tool_and_marks_run_step_exec_cancelled` 使用 Python `Event` 和模拟 blocking tool，只证明：

- 取消令牌进入工具；
- run／step／exec 被标为 cancelled；
- SSE 发出 cancelled。

它没有创建 OS 子进程，因此不能证明进程树清理。D11 必须新增真实进程测试。

### 4.7 trajectory 需要最小前置实现

正式 trajectory schema 和 CLI 计划在 D14，但 D11 已要求“轨迹有取消事件”。建议本阶段只建立最小、可扩展的 append-only 事件记录，至少包含：

- `schema_version: 1`
- `run_id`、`step_id`、`exec_id`、`sandbox_id`
- 事件类型、状态、时间戳和 run 内 sequence

取消请求与最终 cancelled 应可区分；最终 cancelled 只能在进程树清理完成后写入。D14 再冻结完整 schema、事件集合和 CLI，不要提前扩张成完整观测平台。

## 5. 推荐实施顺序

1. **先写失败测试**：tier1 成功、超时、主动取消、断流取消、父＋子进程清理、环境变量清洗和输出上限。
2. **实现最小 tier1 runner**：
   - 独立临时工作区；
   - 仅传契约允许的最小环境变量，不继承 API key、token、`.env`、`PYTHONPATH` 或宿主应用配置；
   - 组合限制 stdout＋stderr，避免管道读取死锁；
   - 超时、取消和输出超限都执行同一套进程树清理；
   - 清理完成后才持久化最终状态。
3. **实现最小平台执行层**：复用 D10 的状态和结果码，不要为了 D11 提前完成节点调度、create 幂等或 TTL；那些分别属于 D15—D20。
4. **接入真实工具链路**：让 allow 工具实际通过平台执行层，保留 ask／deny 前置语义、用户归属和现有 JSON 聊天兼容性。
5. **连接取消令牌**：客户端断开／Esc → `CancellationToken` → 平台 cancel → runner 进程树清理 → exec cancelled → trajectory cancelled。
6. **补最小 trajectory 取消事件**，并保存可复查证据。
7. **更新文档与测试索引**，最后跑全量回归和真实断流验收。

Windows 是当前实测平台。`CREATE_NEW_PROCESS_GROUP` 本身不等于进程树清理；只 `kill()` 直接父进程也不合格。实现需覆盖 runner 启动的子进程，并且只能清理由本平台明确拥有的进程，不得按进程名笼统结束其他进程。

## 6. 必须保留的安全边界

- tier1 是受控执行环境，**不是强安全沙箱**，不能运行或演示不可信恶意代码。
- tier1 没有 OS 级网络隔离；工具级网络 allowlist 不是安全边界。
- 不把审批当成隔离。
- 不让子进程继承宿主完整环境。
- 不用字符串前缀代替 real path 工作区归属判断；完整路径／符号链接负向测试属于 D12，但 D11 不应提前留下明显逃逸入口。
- 不读取、打印或提交 `.env`。实际项目副本根目录已有用户提供的 `.env`，且被 Git 忽略。
- `docs/threat-model.md` 第 21 行“本项目副本不包含真实 `.env`”已过时，后续文档修改时应改成“真实 `.env` 不纳入 Git／证据”，不要披露其内容。
- 所有数据库和进程验收只使用临时目录；不得触碰项目根目录的现有 `*.db`，不得发送外部通知。
- 保留 TTS 既有独立 subprocess 路径，它不是当前 Agent 工具 exec；搜索证据中应解释，而不是为了“零 subprocess”误删 TTS。

## 7. 测试与证据要求

### 7.1 自动化测试

建议新增：

- `tests/integration/test_tier1_runner.py`
- `tests/integration/test_platform_exec_cancel.py`
- 扩充 `tests/test_agent_stream.py`，验证助手到平台的取消传播
- 保留并重跑 `tests/unit/test_platform_sdk.py`

新测试必须登记到 `tests/registry.py`，再同步索引：

```bash
".venv/Scripts/python.exe" scripts/run_tests.py list --doc
".venv/Scripts/python.exe" scripts/run_tests.py audit
```

数据库测试使用 `temporary_sqlite`；网络优先 `none` 或 `mocked`。真实 OS 进程测试要设置短超时和可靠 cleanup，失败时也不能留下子进程。

### 7.2 全量回归

```bash
".venv/Scripts/python.exe" scripts/run_guarded_tests.py backend
```

该 runner 会替换真实模型凭据、隔离临时数据库并阻断外部 socket。D11 不需要消耗真实模型额度。

若修改前端，再运行：

```bash
cd frontend
npm run build
```

### 7.3 真实断流验收

建议增加 `scripts/verify_d11_cancel.py` 或等价脚本，使用真实 HTTP/SSE 和临时目录：

1. 启动测试服务与由 runner 创建的父进程＋子进程。
2. 在工具 running 时断开 SSE 或调用取消接口。
3. 等待 worker 完全结束。
4. 验证平台 exec 为 cancelled。
5. 验证父、子 PID 均不再存活，且没有误杀非平台进程。
6. 验证 trajectory 同时有取消请求和最终 cancelled，顺序正确。
7. 关闭测试服务并清理临时目录。

建议证据：

- `docs/evidence/d11-runner-tests.txt`
- `docs/evidence/d11-live-cancel.txt` 或 JSON
- `docs/evidence/d11-host-exec-search.txt`
- `docs/evidence/d11-test-audit.txt`
- `docs/evidence/d11-backend-tests.txt`

证据中记录实际观测值，不预估、不复制 D10 结果冒充 D11。

## 8. 不要提前扩张的范围

D11 不需要一次完成以下后续任务：

- D12：read／write／ls／shell／python／http 全量工具及完整路径负向测试
- D13：完整审批交互
- D14：完整 trajectory schema 和 CLI
- D15：create 幂等
- D16：TTL／reaper
- D17—D20：节点、租约、调度和完整状态机

但如果 D11 的设计会堵死这些后续任务，应优先保持接口简单、状态语义一致，而不是堆临时代码。

## 9. 完成定义与提交

完成后更新 `docs/journal.md`，记录真实测试数量、真实断流结果、进程清理证据、遗留限制和下一步 D12。

建议提交信息：

```text
d11: 接入 tier1 沙箱执行与全链路取消
```

提交前核对：

```bash
git diff --check
git status --short --untracked-files=all
```

不要提交 `.env`、临时数据库、构建目录、运行日志、测试进程输出或临时工作区。只有测试和证据全部通过后再提交，并在最终回复中明确哪些已实测、哪些仍未实现。
