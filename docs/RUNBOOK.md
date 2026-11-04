# Runbook（D39）

> 照着做就行。每条都给出「怎么判断」「怎么处置」「怎么确认好了」。
> 命令里的 `.venv` 指仓库自带的虚拟环境。

## 0. 先看三样东西

```powershell
# 健康
curl http://127.0.0.1:8000/health
# 指标（有没有积压 / 失败）
curl http://127.0.0.1:8000/metrics
# 审计（谁干了什么、有没有被拒）
curl -H "Authorization: Bearer <token>" "http://127.0.0.1:8000/v1/audit?limit=20"
```

## 1. `/v1` 全部 401

- **判断**：配了 `PERSONAL_AGENT_PLATFORM_TOKEN`，请求没带或带错。
- **处置**：`Authorization: Bearer <token>`；SDK 用 `PlatformClient(token=...)`。
- **确认**：`/v1/queue` 返回 200。

## 2. 建沙箱一直 429 `NO_READY_NODE`

- **判断**：`GET /v1/nodes` 里节点 `accepting=false`（`cordoned` / `draining` / `removed` / 租约过期）。
- **处置**：
  - 租约过期：平台进程活着时 `ensure_default_node()` 会自动续租；若节点是 `removed`，需要重新注册（`POST /v1/nodes`）。
  - cordon / drain：这是运维显式动作，**没有 uncordon**，重新注册同 id 会 +1 代际并恢复接活。
- **确认**：`/v1/nodes` 里 `accepting=true`。

## 3. exec 一直 429 `QUEUE_FULL`

- **判断**：`GET /v1/queue` 的 `queued == max_queued`。
- **处置**：等（按响应里的 `Retry-After`）；或调大 `PERSONAL_AGENT_PLATFORM_QUEUE_SIZE` / `..._MAX_ACTIVE_EXECS` 后重启；或杀掉跑太久的 exec（`POST /v1/exec/{id}/cancel`）。
- **确认**：`queued` 降下来、新的 exec 变 202。

## 4. 排队中的 exec 变 `timed_out`（`QUEUE_TIMEOUT`）

- **判断**：等太久（默认 60s）还没轮到。
- **处置**：提高 `PERSONAL_AGENT_PLATFORM_QUEUE_MAX_WAIT`，或降低并发占用的长任务；这是**设计行为**，不是 bug。
- **确认**：审计里 `exec.timeout` 有记录，且没有进程残留（它从未启动）。

## 5. 取消之后进程还在跑

- **判断**：`tasklist` 里还有沙箱里起的子进程；exec 状态不是 `cancelled`（可能是 `failed / RUNNER_CLEANUP_FAILED`）。
- **处置**：不要伪造状态。看审计与 `platform_execs.error_code`；重启平台会触发 D22 对账（`INTERRUPTED_BY_RESTART`），孤儿目录由对账清理。
- **确认**：重启后 `GET /v1/exec/{id}` 是终态，且 `docs` 里没有新增孤儿目录（对账日志会写数量）。

## 6. 服务起不来 / 端口被占

- **判断**：启动日志 `address already in use`。
- **处置**：`netstat -ano | findstr :8000` 找 PID，确认不是上次的残留进程再结束它。
- **确认**：`/health` 返回 `{"status":"ok"}`。

## 7. 数据库被锁（Windows `WinError 32`）

- **判断**：测试或清理临时目录时报「另一个程序正在使用此文件」。
- **处置**：等的那个 exec 还在收尾；确认 `active_execs` 空了再删目录。**不要**强删正在使用的 sqlite 文件。
- **确认**：`tasklist` 里没有相关 python 进程。

## 8. 磁盘涨得快

- **判断**：`docs/bench/data`、沙箱目录、审计表变大。
- **处置**：审计按 `PERSONAL_AGENT_AUDIT_RETENTION_DAYS`（默认 30 天，启动时清理）；沙箱靠 TTL + reaper；压测产物用 `scripts/bench_platform.py` 的临时目录，结束后删 `docs/bench/data`。
- **确认**：`personal_agent_sandboxes{state="reclaimed"}` 增长、目录大小回落。

## 9. 想看某一次对话到底发生了什么

```powershell
.\.venv\Scripts\python.exe scripts\trace_run.py --db <db> <run_id>
```

输出：run → turn → step → exec 与事件顺序；`exec_started` 行带 `trace=… span=…`，可和日志的 `trace_id` 对上。

## 10. 出事了先做什么

1. 保现场：不要删库、不要重启后立刻清日志。
2. 抓 `/metrics` 与 `/v1/audit`。
3. 看 `docs/journal.md` 最近三天改了什么。
4. 按 `docs/postmortem.md` 的模板写复盘。
