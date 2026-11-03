# D13 审批门设计草案（接 D12 现状）

> 日期：2026-11-03
> 状态：设计草案，尚未实现。D13 的验收是「危险工具拒绝路径有测试」，
> 这份文档只固定决策与边界，避免实现时来回改语义。

## 1. 现状（D12 结束时）

- 策略矩阵已经在 `src/core/tool_registry.py`：`allow` / `ask` / `deny`。
- 流式链路里 `ask` 在产生 exec **之前**就抛 `ToolApprovalRequired`：
  step 状态 `awaiting_approval`，SSE 发 `approval_required`，**不产生 exec**。
- 旧 JSON 聊天在 D13 之前不接审批门（保持 D8 前行为），执行位置已经在 tier1。
- 沙箱工具里 `write_file` / `run_shell` / `run_python` / `http_get` 策略是
  `ask` 且不暴露给模型 —— 现在等于「不可用」。

## 2. 要固定的语义

1. **审批前不产生 exec**：这是 D8 的约束，D13 不能为了「先建 exec 再回滚」
   破坏它。exec 只在批准之后创建，仍然是一次调用对应一条 exec。
2. **批准的是「这一次调用」，不是「这个工具」**：参数在批准时冻结，执行前用
   摘要校验；参数变了必须重新审批。
3. **拒绝就是终态**：step 记 `denied`，不产生 exec，trajectory 记
   `tool_denied` 事件；同一 run 内不再尝试同一工具。
4. **取消联动**：run 被取消（Esc / 断流）时，所有 pending 审批立即过期，
   不产生 exec。
5. **归属**：审批记录挂在 run 上，只有该 run 的 owner 能批准 / 拒绝；
   其他人的请求一律 404（不泄露存在性）。
6. **审批不是隔离**：批准 shell 不等于让 shell 变安全。tier1 仍然只用于
   受信任代码，文档与提示词都要保留这句话。

## 3. 数据模型

```sql
CREATE TABLE IF NOT EXISTS agent_approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL REFERENCES agent_steps(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    arguments_digest TEXT NOT NULL,   -- sha256(规范化参数 JSON)
    arguments_preview TEXT NOT NULL,  -- 截断后给用户看的参数，不含敏感值
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'approved', 'rejected', 'expired')
    ),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    UNIQUE (step_id)
);
```

- 同一 step 只允许一条审批记录（`UNIQUE(step_id)`），重复请求读同一条。
- 决定只能发生一次：`UPDATE ... WHERE status = 'pending'`，受影响行数为 0
  就返回 409。

## 4. 接口与事件

| 能力 | 接口 | 语义 |
| --- | --- | --- |
| 查询待审 | `GET /chat/requests/{request_id}/approvals` | 返回 pending 记录 |
| 决定 | `POST /chat/requests/{request_id}/approvals/{approval_id}` | body `{"decision": "approve"｜"reject"}`；幂等冲突 409 |
| SSE | `approval_required`（含 `approval_id`、工具名、参数预览） | 批准前先发给客户端 |
| SSE | `approval_decided`（含 decision） | 决定后广播，便于多端同步 |

批准 → 执行是**服务端**动作：HTTP 返回 202 后由 worker 创建 exec 并调用
`agent.execute_tool_on_platform(..., require_allow=False)`（批准本身就是放行），
仍然走 tier1 与 trajectory。

## 5. 与 trajectory 的关系

- 新增两种事件：`tool_approval_requested`、`tool_approval_decided`。
- 只有批准之后才出现 `exec_started`；拒绝时只有请求 + 拒绝两条事件，
  **没有 exec 事件**，这样「审批没通过」和「执行失败」在轨迹里不会混淆。

## 6. 必须有的测试

1. `ask` 工具 → 只有 `approval_required`，数据库里没有 exec 行。
2. 批准 → 恰好一条 exec，且 `succeeded`；trajectory 顺序为
   requested → decided(approve) → exec_started → exec_finished。
3. 拒绝 → 没有 exec，step 为 `denied`，trajectory 只有 requested → decided(reject)。
4. 重复批准 / 批准已拒绝的请求 → 409。
5. 别人的 run / approval → 404。
6. 取消 run 时 pending 审批 → `expired`，随后不产生 exec。
7. 参数在审批后被篡改（digest 不匹配）→ 拒绝执行，记 failed。

## 7. 开放问题（实现前要定）

- 审批超时多久？建议 5 分钟，超时置 `expired`，由 reaper 或下一次查询顺带处理。
- 多端并发决定：以先提交的为准，另一侧收到 409 并刷新状态。
- 参数预览要脱敏到什么程度（例如 shell 命令里的 token）？建议复用 trajectory
  的脱敏规则，不新造一套。
- 批准后是否允许「同一 run 内同类工具免审」？不建议：会把一次授权放大成长期
  授权，与「批准这一次调用」矛盾。

## 8. 与 D12 工具的衔接

批准体验定下来之后再决定暴露面：

- `write_file`：可以暴露，参数预览就是路径 + 内容摘要。
- `run_shell` / `run_python`：暴露前要先有「命令预览 + 明确的风险提示」，
  否则用户点「批准」时并不知道自己在批什么。
- `http_get`：先定网络允许集（D13 只做目标提示，真正的策略留给后续）。
