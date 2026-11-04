# 单用户沙箱平台契约 v1（D2 冻结）

> 日期：2026-11-02
> 状态：v1 设计与验收基线；后续实现和契约测试必须与本文件一致。
> D11（2026-11-03）已实现 **tier1 本地执行层**：独立工作区、清洗环境、timeout、
> 输出上限、Job Object 进程树清理、取消传播与最小 trajectory 事件。
> **HTTP 控制面、幂等 create、节点租约、TTL / reaper 仍未实现**（D15—D20）。
> 边界：单用户、多任务、多沙箱、单机控制面；不包含多租户、真实多机集群或计费。

## 1. 术语与通用约定

- **run**：一次助手任务；**step**：run 内一个有序步骤；**exec**：沙箱内一次受控执行。三者使用不同的服务端 ID。
- **sandbox**：绑定一个独立工作区和一个节点的执行环境。`sandbox_id`、`exec_id`、`node_id` 在平台内唯一。
- 时间使用带时区的 RFC 3339；持续时间和字节数使用整数，字段名显式带 `_ms` / `_seconds` / `_bytes`。
- 资源不存在与资源已回收不同：未知 ID 返回 404；仍在 tombstone 保留期内的已回收资源返回 410。
- 除健康检查和 Prometheus 指标外，控制面 API 统一放在 `/v1`。JSON 错误响应形状固定为：

```json
{
  "error": {
    "code": "STABLE_ERROR_CODE",
    "message": "human readable, not machine parsed",
    "retryable": false,
    "request_id": "req_..."
  }
}
```

调用方只依赖 `code` 和 HTTP 状态，不解析 `message`。`request_id` 用于日志关联，不代表请求已幂等去重。

## 2. HTTP API 与 Python SDK

> D15.1（2026-11-03）：下表前六项已实现于 `src/platform_api.py`（`/v1` 路由），
> 由 `platform.db` 与 tier1 执行层支撑；`fn` / `metrics` 仍未实现。
> D17（2026-11-03）：节点接口 `POST /v1/nodes`、`GET /v1/nodes`、
> `POST /v1/nodes/{node_id}/heartbeat` 已实现（`src/platform/node_registry.py`）。
> `/v1` 目前**没有接入登录认证**，只适合本机单用户使用（见威胁模型第 12 节）。

### 2.1 最小接口面

| 能力 | HTTP 接口 | Python SDK | 成功语义 |
| --- | --- | --- | --- |
| 创建沙箱 | `POST /v1/sandboxes` | `create(..., idempotency_key=...)` | 创建或命中幂等记录，返回同一逻辑沙箱 |
| 执行命令 / 工具 | `POST /v1/sandboxes/{sandbox_id}/exec` | `exec(...)` | 接受一次 exec；SDK 默认等待最终状态 |
| 调用注册函数 | `POST /v1/sandboxes/{sandbox_id}/fn` | `fn(name, args, kwargs, ...)` | 创建且仅创建一个 exec，复用 exec 全部语义 |
| 查询 exec | `GET /v1/exec/{exec_id}` | `get_exec(exec_id)` | 返回当前或最终状态，供结果不明时查询 |
| 取消 exec | `POST /v1/exec/{exec_id}/cancel` | `cancel(exec_id)` | 请求取消 queued / running exec |
| 回收沙箱 | `POST /v1/sandboxes/{sandbox_id}/destroy` | `destroy(sandbox_id)` | 停止残留 exec、清理进程树并进入 reclaimed |
| 节点信息 | `GET /v1/nodes` | `nodes()` | 返回租约、generation、cordon / drain 与容量状态 |
| 指标 | `GET /metrics` | `metrics()` | 返回 Prometheus 文本指标，不携带高基数 ID 标签 |

`fn` 只是注册函数的便捷封装，不允许绕过工具注册表、审批、超时、输出限制、取消或 trajectory。函数名必须已注册，参数按 JSON 校验；它不是任意 Python 导入或宿主直跑入口。

### 2.2 create

请求至少支持 `tier`、`workspace_id`、`ttl_seconds`；响应至少包含 `sandbox_id`、`state`、`node_id`、`workspace_id`、`created_at`、`expires_at` 和实际生效的 `ttl_seconds`。

- HTTP 请求头 `Idempotency-Key` 必填，SDK 必须支持调用方显式传入；自动生成的 key 只能在同一次逻辑重试中复用。
- key 的作用域是单用户全局 create 接口，不与 sandbox_id 混用。空 key、超长 key 或非法字符返回 400 `INVALID_IDEMPOTENCY_KEY`。
- 请求指纹由**规范化后的完整请求体**计算：JSON key 排序，省略值与服务端默认值先规范化，再比较；不能仅比较部分字段。

### 2.3 exec / fn

请求至少支持 `run_id`、`step_id`、`timeout_ms`、`max_output_bytes` 和命令 / 工具输入；响应返回 `exec_id`、`sandbox_id`、`state` 以及实际生效的限制值。

- `POST` 接受 exec 后返回 202 和 exec 资源；SDK 的 `exec` / `fn` 默认轮询或等待到终态后返回 `ExecResult`，同时保留 `get_exec` 供恢复查询。
- v1 默认 `timeout_ms = 30000`，允许范围 `1…300000`；默认 `max_output_bytes = 65536`，允许范围 `1…1048576`。上限按 stdout 与 stderr 合计计算。
- 最终响应至少包含 `state`、`exit_code`（不适用时为 null）、`stdout`、`stderr`、`started_at`、`finished_at`、`truncated` 和稳定结果码。
- 非零退出码对应 `failed / EXEC_FAILED`；超时对应 `timed_out / EXEC_TIMED_OUT`；取消对应 `cancelled / EXEC_CANCELLED`。这些是已接受 exec 的执行结果，不统一伪装成 HTTP 500。
- 输出超过上限时终止整个进程树，exec 进入 `failed / OUTPUT_LIMIT_EXCEEDED`；只保留上限内的输出前缀，`truncated = true`，不得静默宣称成功。
- 每次终态迁移前都必须发起进程树清理；清理完成后再持久化最终状态。退出码为 0 也只代表命令成功，不扩大 tier1 的安全承诺。

### 2.4 destroy 与查询

- destroy 是可重复请求：目标为未回收沙箱时执行回收；目标已经 reclaimed 时返回相同 tombstone，不创建新沙箱。
- 已回收沙箱上的 create 以外操作返回 410 `SANDBOX_GONE`；从未存在或 tombstone 已清除的 ID 返回 404 `SANDBOX_NOT_FOUND`。
- exec 查询在其沙箱回收后仍按 exec tombstone / trajectory 保留策略可读；沙箱回收不得级联删除审计轨迹。

## 3. create 幂等与 exec 重试边界

### 3.1 create 必须原子幂等

> D15（2026-11-03）：控制面服务已实现（`src/platform/sandbox_service.py`，
> 独立库 `platform.db`）。HTTP `/v1/sandboxes` 入口尚未实现，SDK 仍打内存替身。

| 情况 | 结果 |
| --- | --- |
| 同 key + 同规范化请求体，顺序重试 | 返回同一 `sandbox_id`，不得重复分配容量或创建工作区 |
| 同 key + 同规范化请求体，并发请求 | 原子约束只允许一个创建者，其余请求等待或读取同一结果 |
| 同 key + 不同规范化请求体 | 409 `IDEMPOTENCY_CONFLICT`，不得复用旧沙箱 |
| 控制面重启后，同 key 重试 | 持久化记录仍生效，行为与重启前一致 |

实现必须以数据库唯一约束和同一事务内的状态变更保证原子性，不能依赖进程内字典或“先查再插”。

- 活跃沙箱（尚未 reclaimed）的幂等记录**不得过期**。
- 沙箱回收后，幂等记录与必要 tombstone 默认继续保留 24 小时；实现可配置更长时间，但不能短于 tombstone 保留期。
- 两者均到期并原子清除后，旧 key 可按新请求处理并创建新沙箱；客户端不得跨保留期复用 key。
- 首次请求已接受但响应丢失时，客户端应使用相同 key 和完全相同的逻辑请求体重试 create。

### 3.2 exec 不默认承诺可安全重放

- exec / fn 可能写文件、发请求或产生其他副作用；普通请求 ID、trace ID 和 exec ID 都不等于幂等 key。
- v1 不为 exec 提供自动去重或至少一次重放承诺。超时、断连或 5xx 导致结果不明时，调用方必须先 `get_exec(exec_id)`；若连 exec_id 都未收到，只能按业务幂等键和副作用记录人工 / 上层对账，不能盲目重复执行。
- 若未来增加 exec 幂等，必须单独定义请求指纹、结果恢复和副作用边界；不得借 create 的幂等记录隐式实现。

## 4. 状态机、TTL 与取消

### 4.1 exec 状态机

```text
queued → running → succeeded
                 → failed
                 → cancelled
                 → timed_out
queued ─────────→ cancelled
```

- `succeeded / failed / cancelled / timed_out` 均为终态，不可再迁移。
- queued 与 running 都可取消。queued 尚未启动进程时直接落 cancelled；running 必须等 runner 报告进程树已终止后才落 cancelled。
- cancel 对已经 cancelled 的 exec 幂等返回当前结果；对其他终态返回 409 `EXEC_NOT_CANCELLABLE`。竞争时以先提交的状态迁移为准。
- 客户端取消等待不等于取消 exec：SDK 停止等待时必须显式调用 cancel，除非调用方明确选择仅停止等待。

### 4.2 sandbox 状态机

> D16（2026-11-03）：TTL 回收与 tombstone 已实现于 `src/platform/reaper.py`。
> `sweep_once()` 先取消该沙箱的活动 exec（进程树由执行器清理），再置
> `reclaimed` 并写 `reclaim_reason=ttl_expired`；tombstone 与幂等记录默认保留
> 24 小时（`PERSONAL_AGENT_TOMBSTONE_HOURS` 可覆盖），到期在同一事务里清除。
> API 访问做过期兜底：reaper 还没跑时，访问一个已过期的沙箱会先触发同一条
> 回收路径再返回 410。后台线程间隔 `PERSONAL_AGENT_PLATFORM_REAPER_INTERVAL`
> （默认 60 秒），测试环境用 `PERSONAL_AGENT_PLATFORM_REAPER=0` 关闭。

```text
created → running ↔ idle
created / running / idle → failed
created / running / idle → reclaimed
failed → reclaimed
```

> D20（2026-11-04）：状态机已实现于 `SandboxService.transition()`。
> `agent_sandboxes` 的 CHECK 从 `('created','reclaimed')` 重建为完整五态，
> 旧库原地迁移（建新表 → 拷贝 → 删旧表 → 改名，保住 idempotency 的外键）。
> `begin_exec` 让 created / idle → running，`end_exec` 在活动 exec 归零时
> running → idle；`failed` 只允许回收，exec 会被拒（409）。`reclaimed` 是
> 终态，任何出边都返回 409 `INVALID_STATE_TRANSITION`。

- created 表示已分配但尚无活动 exec；running 表示至少一个活动 exec；idle 表示可复用但无活动 exec。
- 单个命令非零退出不自动使沙箱 failed；runner 崩溃、工作区不可用或运行时失去所有权才使沙箱 failed。
- `reclaimed` 是终态。任何复活、反向迁移或跳过清理的迁移均返回 409 `INVALID_STATE_TRANSITION`。
- `ttl_seconds` 是从创建接受时间起算的最长存活时间。到期后 reaper 先取消活动 exec、清理进程树，再将沙箱置为 reclaimed；后续使用返回 410 `SANDBOX_GONE`。
- tombstone 至少保存 `sandbox_id`、最终状态、回收原因和 `reclaimed_at`，默认保留 24 小时；trajectory 的保留与删除独立处理。

### 4.3 全链路取消

固定链路：**客户端断开 / Esc → 助手取消当前 run → 平台 cancel exec → runner 终止进程树 → trajectory 记录 cancelled**。

- 断开 HTTP / SSE 不能只停止响应；助手必须向平台传播取消。
- 平台必须将取消送到当前节点和 runner，不能只修改数据库状态。
- runner 必须覆盖工具启动的子进程；只终止直接父进程不算完成。
- trajectory 先记录取消请求，最终 cancelled 事件只能在进程树清理完成后写入；若清理失败，应记录 failed 和明确错误，不能伪造 cancelled 成功。

## 5. 节点租约与最小调度

### 5.1 generation 与过期

> D17（2026-11-03）：注册与心跳已实现于 `src/platform/node_registry.py`。
> 每次注册 generation +1；心跳必须匹配当前 generation，否则 409
> `NODE_GENERATION_STALE`，且不会改写 `last_heartbeat`。
> **租约过期判定（连续 3 个周期无心跳 → `not_ready`）属于 D18**，D17 只记录
> `last_heartbeat` 与状态字段。
> D18（2026-11-03）：租约判定已实现。节点状态由 `last_heartbeat` 推导：超过
> `heartbeat_interval_seconds × 3` 即 `not_ready`（响应带 `lease_seconds`、
> `lease_expires_at`、`seconds_since_heartbeat`、`expired`）。有效 generation
> 的心跳会把状态刷新回 `ready`；旧 generation 的心跳继续 409，且不改写心跳
> 时间。过期只影响调度资格，不做进程清理（沙箱回收是 D16 的事）。

- 节点每次注册都获得新的、单调不可复用的 `generation`；即使 `node_id` 相同，旧 generation 也不能续新实例的租约。
- 心跳与续约必须同时匹配 `node_id + generation`，并携带服务端可校验的租约信息。
- 旧 generation 的延迟心跳 / 续约返回 409 `NODE_GENERATION_STALE`；过期节点保持 not_ready，不得被旧请求复活。
- 节点过期只影响调度资格，不直接假设其所有进程已消失；启动对账与孤儿清理由平台所有权标识另行保证，非本平台进程不得误清。

### 5.2 least-loaded

只有同时满足以下条件的节点可接收**新沙箱**：

1. 租约有效且 ready；
2. 未 cordon、未 drain；
3. 已分配且尚未 reclaimed 的沙箱数低于节点容量上限，包括 failed 但尚待清理的沙箱。

调度选择活动沙箱数最少的节点；并列时按 `node_id` 字典序稳定选择。容量检查、全局名额占用和节点分配必须在同一原子操作中完成，防止并发超分。

- cordon：不接新沙箱；已有 exec 按既有契约继续或显式取消。
- drain：不接新沙箱，等待存量活动 exec 和沙箱清理完成后才允许 remove；drain 不等于立即杀进程。
- 单用户的所有 run 共享全局沙箱并发上限；创建时超限返回 429 `CAPACITY_EXCEEDED`，回收后释放名额。
- 无可用节点返回 429 `NO_READY_NODE`。D30 前不隐式无限排队；D30 增加有界队列后，队列满返回 429 `QUEUE_FULL`，queued 请求仍须可取消。
- 该策略不声称感知真实 CPU / 内存，也不是真实多机调度。

> D19（2026-11-04）：cordon / drain / remove 已实现
> （`POST /v1/nodes/{node_id}/cordon|drain|remove`）。节点视图新增
> `cordoned` / `draining` / `removed` / `accepting` / `active_sandboxes`；
> `accepting=false` 时 `POST /v1/sandboxes` 返回 429 `NO_READY_NODE`。
> `remove` 必须满足「已 drain + 该节点未回收沙箱数为 0」，否则 409
> `INVALID_STATE_TRANSITION`（`retryable=true`），且可重试。cordon / drain
> 都不杀存量 exec（有真实进程测试）。仍缺：least-loaded 调度、全局/节点容量
> 原子占用（D20/D21）。
>
> D21（2026-11-04）：配额与容量已实现。`agent_sandboxes` 增加 `tenant_id`
> （默认 `default`，可用 `X-Tenant-Id` 头覆盖）；创建时在**同一事务**里检查
> 「该租户未回收沙箱数 < `PERSONAL_AGENT_SANDBOX_QUOTA`（默认 8）」与
> 「该节点未回收沙箱数 < 节点 `capacity`」，超限返回 429 `CAPACITY_EXCEEDED`
> （`retryable=true`）。幂等重放不占新名额；回收后名额自动释放（按 `state !=
> 'reclaimed'` 计数）。并发创建由 `BEGIN IMMEDIATE` 串行化，测试验证 10 并发
> 只成功配额数个。仍缺：真实多租户身份（`/v1` 无认证）、least-loaded 调度。

## 6. 稳定错误码

| HTTP | code | 含义与重试规则 |
| ---: | --- | --- |
| 400 | `INVALID_ARGUMENT` | 参数、超时或输出限制非法；修改请求后再试 |
| 400 | `INVALID_IDEMPOTENCY_KEY` | key 缺失或格式非法 |
| 404 | `SANDBOX_NOT_FOUND` / `EXEC_NOT_FOUND` | 资源未知或 tombstone 已清除 |
| 409 | `IDEMPOTENCY_CONFLICT` | 同 key 不同请求体；不得自动换请求体重试 |
| 409 | `INVALID_STATE_TRANSITION` | 非法沙箱 / exec 状态迁移 |
| 409 | `EXEC_NOT_CANCELLABLE` | exec 已到非 cancelled 终态 |
| 409 | `NODE_GENERATION_STALE` | 节点代际已过期，必须重新注册 |
| 410 | `SANDBOX_GONE` / `EXEC_GONE` | 资源已回收或过期，需重新创建 / 查询上层状态 |
| 429 | `CAPACITY_EXCEEDED` / `NO_READY_NODE` / `QUEUE_FULL` | 背压；只有收到有界排队语义后才可按退避策略重试 |

内部故障使用 500 `INTERNAL_ERROR` 并保留 request_id；不得把已知超时、取消、容量和状态冲突吞成 500。网络层是否可重试仍遵守第 3 节，不能只看 `retryable` 就重放 exec。

## 7. tier1 执行与数据边界

- tier1 仅用于受信任代码或开发演示，是受控执行环境，**不是恶意代码安全隔离**。审批也不能替代隔离。
- 每个沙箱使用平台分配的独立工作区。v1 文件工具只接受相对路径；先解析 real path，再验证仍位于该工作区内。绝对路径、`..` 穿越、指向工作区外的符号链接均拒绝；不能用字符串前缀代替归属判断。
- runner 不复制宿主完整环境。默认仅传递操作系统 / 运行时所需的最小允许集：`PATH`、`PATHEXT`、`SYSTEMROOT`、`WINDIR`、`COMSPEC`、`TEMP`、`TMP`、`LANG`、`LC_ALL`；不存在则省略。API key、token、`.env`、`PYTHONPATH` 和宿主应用配置默认不继承，v1 exec 不提供任意环境变量注入。
- 默认不授予自由网络工具；`http` 必须由工具策略显式允许。tier1 没有 OS 级网络隔离，shell / python 仍可能联网，因此该策略不是安全边界，也不得用于不可信代码演示。
- timeout、输出上限、取消和进程树清理由 runner 强制执行，不能依赖模型或被启动进程配合。
- 工具参数、结果、日志和 trajectory 只保存调试所需字段；环境变量与敏感值不得原样写入。输出上限同时约束持久化，避免无限放大磁盘。
- tier2 只能声称已通过宿主目录、网络策略和敏感环境变量负向测试验证的范围。

## 8. trajectory 与 v1 兼容规则

- 每条 trajectory 事件显式携带整数 `schema_version: 1`，并关联 `run_id`；工具执行事件同时关联 `step_id`、`exec_id`、`sandbox_id`。
- 每个 run 内有单调递增的 `sequence`；事件必须有类型、时间戳和状态。最终状态必须能与 exec / sandbox 的最终记录对账。详细字段和 JSONL 形状在 D14 的 `docs/trajectory-schema.md` 冻结。
- D10 已冻结一组 v1 HTTP / SDK 请求与响应 fixture，覆盖 create、exec、fn、destroy、metrics、cancel 和典型错误；共享 fixture 位于 `sdk/fixtures/v1/contracts.json`，Python SDK 位于 `sdk/python/personal_agent_platform`，可用 `python tests/unit/test_platform_sdk.py` 重跑契约测试。fixture 使用内存传输替身，不代表平台 HTTP 服务已经实现。
- v1 内可新增可选响应字段，旧调用方必须忽略未知可选响应字段；不得改变既有字段类型、单位、默认值或状态含义。
- v1 内新增可选请求字段时，省略它的行为必须与旧版本完全一致。新增必填请求字段、删除字段、收紧既有合法输入或改变状态 / 错误语义，必须升级主版本。
- 本周期只保留 v1 兼容性证据，不承诺并行维护多个主版本。真实训练接入与 trajectory 回放不在契约内。
