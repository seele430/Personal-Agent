# trajectory 事件 schema v1（D14 冻结）

> 日期：2026-11-03
> 状态：**v1 已冻结**。后续只允许新增可选字段或新增事件类型；已有字段名、
> 类型和顺序语义不得改变。完整平台契约见 `docs/platform-contract.md` 第 8 节。

## 1. 存储

事件落在聊天库的 `agent_trajectory_events`（`src/core/run_store.py` 的
`init_run_schema`）。随 run 级联删除（`ON DELETE CASCADE`）。

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | TEXT | `evt_` 前缀，全局唯一 |
| `run_id` | TEXT | 必填，指向 `agent_runs` |
| `sequence` | INTEGER | run 内从 1 单调递增，`UNIQUE(run_id, sequence)` |
| `schema_version` | INTEGER | 固定 1 |
| `event_type` | TEXT | 见第 3 节 |
| `status` | TEXT | 事件当下的状态；无状态事件可为空 |
| `step_id` | TEXT? | 属于某个 step 的事件必填 |
| `exec_id` | TEXT? | exec 相关事件必填 |
| `sandbox_id` | TEXT? | 沙箱相关事件必填 |
| `payload_json` | TEXT? | 调试用 JSON 对象 |
| `created_at` | TEXT | UTC ISO 8601 |

## 2. 顺序与终态规则

1. `sequence` 在 run 内严格递增；并发写入冲突时重试，不丢事件。
2. `run_started` 是第一条事件；`run_finished` 是最后一条。
3. 审批：`tool_approval_requested` 一定早于 `tool_approval_decided`；
   **只有 `approved` 之后才允许出现 `exec_started`**。
4. 取消：`exec_cancel_requested` 一定早于 `exec_finished/cancelled`；
   `exec_finished/cancelled` 只有在 runner 进程树清空之后才允许写入。
   清理失败时写 `exec_finished/failed` + `RUNNER_CLEANUP_FAILED`，**不得**写成
   cancelled。
5. 同一个 exec 只有一条 `exec_started` 和一条 `exec_finished`。
6. 事件是 append-only：不改写历史事件，状态变化通过新事件表达。

## 3. 事件类型（v1）

| event_type | status 取值 | 关联字段 | 含义 |
| --- | --- | --- | --- |
| `run_started` | `running` | run_id | run 被接受 |
| `run_finished` | `succeeded` / `failed` / `cancelled` | run_id | run 终态 |
| `tool_approval_requested` | `pending` | run / step | 需要用户批准 |
| `tool_approval_decided` | `approved` / `rejected` / `expired` | run / step | 用户或超时给出结果 |
| `exec_started` | `running` | run / step / exec / sandbox | runner 已启动 |
| `exec_cancel_requested` | `cancel_requested` | run / step / exec / sandbox | 收到取消 |
| `exec_finished` | `succeeded` / `failed` / `cancelled` / `timed_out` | run / step / exec / sandbox | exec 终态 |

新增事件类型时：旧读取方必须能忽略未知类型（未知即跳过），因此不得把「未知
类型」当成错误。

## 4. payload 约定

- 只放**调试所需**字段：`exit_code`、`error_code`、`cleanup`、`duration_ms`、
  `truncated`、`approval_id`、`requested_at`、`error_type` 等。
- 不放环境变量、凭据、完整 stdout/stderr、完整工具参数。
- 单个 payload 序列化后不超过 2 KB；超长的用户内容留在 `agent_steps`，
  事件只引用 id。
- 参数预览（`arguments_preview`）在审批表里，不进 trajectory payload。

## 5. JSONL 导出形状

每行一个事件对象，字段与表列一致、`payload` 为嵌套对象（不是字符串）：

```json
{"id":"evt_...","run_id":"run_...","sequence":1,"schema_version":1,
 "event_type":"run_started","status":"running","step_id":null,
 "exec_id":null,"sandbox_id":null,"payload":null,
 "created_at":"2026-11-03T08:00:00+00:00"}
```

D14 的 `scripts/trace_run.py --json` 输出的是「run + 事件 + 审批」对象；
按行导出留给 D26 的结构化日志。

## 6. 读取入口

- 代码：`RunStore.list_events(run_id)`（按 sequence 升序）。
- CLI：`python scripts/trace_run.py <run_id>`（文本），`--json`（机器可读），
  `--request-id <id> --owner <user>`（按聊天请求查）。
- 渲染：`src/core/trajectory_view.py`，只读，不产生任何副作用。

## 7. 保留与删除（D14 之后）

- 当前随 run 级联删除，没有单独保留期。
- TTL / 归档属于 D16—D20 的 reaper 范围；届时必须保持「沙箱回收不级联删除
  审计轨迹」这条契约（`docs/platform-contract.md` 4.4）。
