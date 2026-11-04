# 架构（D40）

## 一张图

```text
用户/前端
   │  HTTP / SSE
   ▼
┌─────────────────────────────────────────────┐
│ FastAPI (src/main.py)                       │
│  ├─ /chat        助手流式链路               │
│  ├─ /v1/*        平台控制面（Bearer 认证）  │
│  ├─ /metrics     Prometheus 文本            │
│  └─ 中间件：限流 → 认证 → trace → 路由      │
└──────────────┬──────────────────────────────┘
               │ 进程内调用（同一套实现）
               ▼
┌─────────────────────────────────────────────┐
│ 平台服务层                                   │
│  ├─ SandboxService   状态机 / TTL / 配额    │
│  ├─ ExecQueue        并发闸门 + 有界队列     │
│  ├─ NodeRegistry     注册 / 租约 / 驱逐      │
│  ├─ ExecStore        执行记录               │
│  ├─ AuditLog         审计                   │
│  └─ reconcile        启动对账               │
└──────────────┬──────────────────────────────┘
               │ 子进程（清洗环境 + Job Object）
               ▼
┌─────────────────────────────────────────────┐
│ tier1 runner (src/platform/tier1_runner.py) │
│  ├─ tool: 调工具 handler                    │
│  └─ command: 执行 argv                      │
└──────────────┬──────────────────────────────┘
               │ 工作区（沙箱目录）
               ▼
           文件系统
```

## 三个 trade-off

### 1. Windows Job Object，而不是 `taskkill /T`

- **选它**：`KILL_ON_JOB_CLOSE` 保证「句柄一关，整棵树必死」，这是「取消必须真的杀掉进程树」的前提。
- **代价**：Windows-only；要在 runner 启动前把子进程塞进 job（所以有 stdin gating 的设计）。
- **放弃的方案**：`taskkill /T` —— 尽力而为、有竞态、杀完还得自己确认。

### 2. 进程内队列与限流，而不是 Redis / 分布式锁

- **选它**：单机单用户，外部依赖为零；队列/限流/活跃表都是进程内的，行为可预测。
- **代价**：多 worker 各算各的 —— 写进 SLO 与威胁模型，不假装是全局限流。
- **放弃的方案**：Redis 队列 —— 为了一个单机产品引入第二个有状态服务，运维成本和收益不成正比。

### 3. 单个 SQLite 库，而不是服务化存储

- **选它**：沙箱、执行、审计、节点同库；事务能原子地覆盖「检查配额 + 插入沙箱」。
- **代价**：写并发靠 `BEGIN IMMEDIATE` 串行化；多进程写会争锁。
- **放弃的方案**：Postgres —— 单机上没有收益，反而多一个要备份的东西。

## 一次 exec 的完整路径

1. `POST /v1/sandboxes/{id}/exec` → 认证 → 限流 → 校验 → 建 `platform_execs` 行（queued）
2. `ExecQueue.submit()` → 有空位就起线程，否则排队（满则 429 `QUEUE_FULL`）
3. `_run_exec`：`begin_exec`（沙箱 → running）→ 生成 traceparent → 启动 runner 子进程
4. runner：清洗环境、进 Job Object、执行 argv（`TRACEPARENT` 在环境里）
5. 结束：`store.finish` 写终态 → `end_exec`（沙箱 → idle）→ 出队下一条
6. 轨迹：`exec_started` / `exec_finished` 带 traceparent 与 cleanup 信息
