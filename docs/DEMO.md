# D40 演示口径（D11–D30.2）

> 这份文档只写**能跑、能看、能核**的东西。每条命令都在本机实跑过；
> 如果某条在你的机器上不成立，以仓库里的证据文件为准（`docs/evidence/`）。

## 1. 一句话定位

**Personal Agent 是一个跑在本地的个人 AI 工作台；D11–D30 把「工具执行」从宿主进程搬进了
一个可杀、可验、可查、可对账的 tier1 沙箱平台。**

## 2. 三种长度的讲法

**30 秒版**

> 模型调工具时，真正执行命令的是宿主进程还是隔离进程？这个项目选了后者：每条命令都在
> 独立 runner 里跑、在 Windows Job Object 里被管辖，取消时整棵进程树一起结束；
> 每次执行都落轨迹、带 traceparent；服务重启后中断的执行会被对账成确定终态。
> 全量 745 个用例。

**3 分钟版（四个「为什么」）**

1. **为什么沙箱**：工具里有 shell / python，跑在宿主里等于模型直接拿你的机器。
2. **为什么审批门**：写文件、发请求这类有副作用的事，用户点「批准」之前**不产生 exec**。
3. **为什么取消**：用户按 Esc 时，只停「等结果」不算取消；必须一路传到 runner，
   连子进程一起杀掉，才能记 `cancelled`。
4. **为什么对账**：平台会崩、会重启；重启后 `queued/running` 的执行不能悬着，
   统一落 `failed / INTERRUPTED_BY_RESTART`，孤儿目录清理掉。

## 3. 演示前准备

```powershell
cd "C:\Users\123\Personal Agent"
$env:PYTHONIOENCODING = "utf-8"
# 演示脚本自己选端口、自己建临时目录，不碰你的真实数据
```

前置：`.venv` 可用（`.\.venv\Scripts\python.exe -c "import src.main"` 能过）。

## 4. 演示 A：助手全链路（一句话 → 3 个工具 → 轨迹）

```powershell
.\.venv\Scripts\python.exe scripts\verify_g2_chain.py
```

**看什么**

- `tool_started_events: 3` / `tool_finished_events: 3`
- 三条 exec 全部 `succeeded`，各带 `sandbox_id`
- `traceparents` 三条：**同一个 trace-id、三个不同 span-id**
- 末尾直接打印 `trace_run.py` 的输出：run → turn → 3 个 tool step → 终答

**一句话解释**

> 这条链里，模型只负责「要调用什么」；真正跑命令的是沙箱 runner，事件先写轨迹再出结果。

## 5. 演示 B：全链路取消

```powershell
.\.venv\Scripts\python.exe scripts\verify_d11_cancel.py
```

**看什么**：取消请求先写 `exec_cancel_requested`，进程树清理完成后才写 `exec_finished/cancelled`；
顺序反了就是 bug。

## 6. 演示 C：平台 HTTP 面 + SDK

```powershell
.\.venv\Scripts\python.exe scripts\verify_platform_v1.py
```

**看什么**：真实端口上的 create → exec → cancel → destroy，SDK 全程走 HTTP；
`result_code` 依次是 `EXEC_SUCCEEDED` / `EXEC_CANCELLED` / 回收后 410。

## 7. 演示 D：重启对账

```powershell
.\.venv\Scripts\python.exe scripts\verify_d22_reconcile.py
```

**看什么**：起服务 → 建沙箱 → 杀掉 → 人为造「删了工作区 + 插一条 running exec + 放个孤儿目录」
→ 重启 → 直接读库确认：

```text
sandbox_state: failed / workspace_missing
exec_state:    failed / INTERRUPTED_BY_RESTART
orphan_dir_removed: true
```

**一句话解释**：重启后不假装「它还在跑」，也不假装「它被取消了」—— 落 failed 才是诚实的。

## 8. 演示 E：认证 / 限流 / 审计

```powershell
# 未配置令牌：本机可调（启动日志会警告）
.\.venv\Scripts\python.exe -c "from fastapi.testclient import TestClient; from src.main import app; print(TestClient(app).get('/v1/queue').json())"

# 配置令牌后：没有 Authorization → 401
$env:PERSONAL_AGENT_PLATFORM_TOKEN = "demo-token"
.\.venv\Scripts\python.exe -c "from fastapi.testclient import TestClient; from src.main import app; print(TestClient(app).get('/v1/queue').status_code)"
```

**看什么**：401 的错误信封；配上令牌后 `200`；`GET /v1/audit` 能看到 `auth.denied` 留痕。

## 9. 演示 F：背压（队列）

```powershell
.\.venv\Scripts\python.exe scripts\verify_d30_queue.py
```

**看什么**（脚本自己起真实服务、自己设限）：

```json
{
  "first": "queued", "second": "queued",
  "third_http": 429, "third_code": "QUEUE_FULL", "retry_after": "5",
  "queue": {"active": 1, "queued": 1, "max_active": 1, "max_queued": 1, "max_wait_seconds": 2.0},
  "second_final": "timed_out"
}
```

**一句话解释**：不隐式无限排队；数量（队列满 429）和时间（等 2 秒落 `timed_out`）都有上限。

## 10. 数字与证据

| 项 | 值 |
|---|---|
| 全量回归 | **82 文件 / 745 用例 PASS**（`scripts/run_guarded_tests.py backend`） |
| 证据 | `docs/evidence/`（每轮一份 txt/json） |
| 契约 | `docs/platform-contract.md`（含每轮实现注记） |
| 轨迹 schema | `docs/trajectory-schema.md`（v1 冻结） |
| 威胁模型 | `docs/threat-model.md`（含 D25 认证边界） |

## 11. 诚实清单（被问就必须承认）

1. **tier1 不是安全沙箱**：它隔离了工作目录和环境变量，但没有网络隔离、没有文件 jail。
2. **`/v1` 未配置令牌时无认证**，只监听 `127.0.0.1`；任何本机进程都能调。
3. **限流和队列都是进程内的**，多 worker 各算各的，不是全局限流/全局调度。
4. **审计不记命令正文**，只记谁在什么时候对哪个资源做了什么。
5. **SSE 断连取消**依赖客户端显式 cancel（SDK 已封装），不是靠 TCP 断开嗅探。

## 12. 常见追问与答法

**Q：真的是沙箱吗？**
A：不是 VM/容器级沙箱，是「隔离执行层」：独立工作区、清洗过的环境变量、Job Object 管辖的进程树、
超时/取消/输出上限。网络没有隔离 —— 这点写在威胁模型里。

**Q：为什么用 Windows Job Object 而不是 taskkill？**
A：`taskkill /T` 只能尽力而为，且存在竞态；Job Object 的 `KILL_ON_JOB_CLOSE` 能保证
「句柄关闭时整棵树一定结束」，这是取消语义能落地的前提。`taskkill` 只作兜底。

**Q：模型一次要调 3 个工具怎么办？**
A：默认一次只允许 1 个（产品约束）；`PERSONAL_AGENT_MAX_TOOL_CALLS_PER_TURN` 可放到 8，
演示用 3。提醒类工具始终单独限 1 条。

**Q：745 个用例都测了什么？**
A：执行层（超时/取消/输出截断/进程树）、审批门、轨迹、幂等、HTTP 面、TTL/reaper、
节点租约/cordon/drain、状态机、配额、对账、G2 全链路、traceparent、认证、限流审计、队列。
分层与隔离审计见 `tests/TEST_LAYERS.md` 与 `scripts/run_tests.py audit`。

## 13. 录屏建议

1. 先跑 **A**（最有冲击力：一句话 3 个工具 + 轨迹）；
2. 再跑 **B**（取消，强调事件顺序）；
3. 最后跑 **D**（重启对账，强调「不伪造终态」）。
4. 每次演示后 `git checkout -- docs/evidence`，避免证据文件被覆盖。
