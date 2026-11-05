# 设计：意图路由（Router）

> 版本：v1（2026-11-05）
> 状态：W1.1 设计稿；实现见 `src/routing/`
> 原则：Router 只给约束，不做执行；平台只按约束执行，不做语义判断。

---

## 1. 目标与非目标

### 目标

- 把「用户想做什么」变成结构化决策：intent、风险、能力、模型策略、执行约束、回退白名单。
- 决策可解释、可落轨迹、可评估。
- 与平台**职责分离**：Router 不选节点、不查配额、不创建沙箱。

### 非目标

- 不做节点选择、扩缩容、生命周期管理（属平台）。
- 不降低权限：高风险操作必须过确定性审批闸门。
- 不覆盖视频/外部作业（本阶段不做 `external_job`）。
- 不追求一次覆盖所有意图：先 5 类真实能力，`clarify` 是决策动作不是 intent。

---

## 2. Intent

| intent | 含义 | 默认能力 | 默认执行类型 |
|---|---|---|---|
| `general.chat` | 闲聊、问答、解释 | `chat` | 无 |
| `data.analysis` | 数据分析、统计、图表 | `sandbox_exec` | `local_sandbox` |
| `code.execute` | 跑脚本/命令/复现报错 | `sandbox_exec` | `local_sandbox` |
| `file.operate` | 读写/创建/删除/重命名文件 | `sandbox_exec` + 文件工具 | `local_sandbox` |
| `platform.inspect` | 查指标、节点、队列、沙箱 | `platform_read` | 无 |
| `reminder.manage` | 提醒/定时 | `reminder` | 无 |

`clarify`：当最高置信度低于阈值、或前两名差距过小、或高危语义歧义时，返回 `decision="clarify"`，附一个具体的澄清问题。

---

## 3. RouteDecision（唯一对外结构）

```python
@dataclass(frozen=True)
class RouteDecision:
    decision: Literal["route", "clarify"]   # clarify 时 intent=None
    intent: str | None
    confidence: float                        # 0..1，已校准
    risk: Literal["low", "medium", "high"]
    requires_approval: bool
    capability: str                          # chat / sandbox_exec / platform_read / reminder
    execution_type: str | None               # local_sandbox / None
    model_policy: Literal["cheap", "strong", "local"]
    constraints: dict                        # 超时、资源上限、允许的工具白名单
    fallback: tuple[str, ...]                # 允许的回退 intent，按优先级
    candidates: tuple[tuple[str, float], ...]# 前 3 名及分数，便于解释
    reason: str                              # 命中了哪条规则 / 哪个分类器
    source: Literal["rule", "classifier", "llm", "clarify"]
```

不变量：

1. `requires_approval=True` 时，平台必须走审批；Router 不能把它设回 False。
2. `decision="clarify"` 时不得携带 `execution_type`。
3. Router **不产出** `node_id`、`sandbox_id`、`queue_position`。

---

## 4. 决策流程

```
normalize（去空白、截断、语言检测）
  → 规则层（关键词/正则，< 1 ms）
     命中且唯一 → 直接产出
     命中多个 → 进入比较
  → 分类器层（embedding + 线性模型，可选）
     输出各 intent 概率，与规则分数按来源分别校准
  → 策略层
     阈值 / 差距 / 高危规则 / 回退白名单 / 模型策略
  → decision（route 或 clarify）
```

规则与模型的置信度**分别校准**（规则是确定性分数，模型是概率），不直接相加。初版以规则为主，分类器只在规则未命中时兜底。

---

## 5. 风险与审批

| 风险 | 判定 | 处理 |
|---|---|---|
| low | 只读、查询、聊天 | 直接执行 |
| medium | 写文件、跑任意脚本 | 记录 + 可配置审批 |
| high | 删除/覆盖/格式化/提权/改配置/泄露密钥 | **强制审批**（确定性） |

硬规则：

- 高危判定是**规则集合**，不是模型概率；模型只能把风险往高里提，不能往低里降。
- 审批闸门在平台侧实现，Router 只传 `requires_approval` 与 `risk`；闸门不读置信度。

---

## 6. 平台契约

Router 提交的是**约束**，不是执行计划：

```json
{
  "capability": "sandbox_exec",
  "execution_type": "local_sandbox",
  "constraints": {
    "timeout_ms": 120000,
    "allowed_tools": ["python", "write_file", "list_dir"],
    "max_output_bytes": 1048576,
    "artifact": {"required": true, "kinds": ["png", "csv"]}
  }
}
```

平台职责：

- 准入：租户/全局配额、容量、队列；
- 选择节点（Router 不参与）；
- 创建/执行/取消/回收；
- 按实时状态执行回退（例如队列满时排队或快速失败，取决于平台配置）。

平台可以拒绝不满足的约束（如超时超过上限），返回明确错误码；Router 不应假设约束一定被接受。

---

## 7. clarify 策略

触发条件（任一）：

1. 最高置信度 < 0.55；
2. 第一名与第二名差距 < 0.15；
3. 命中高危规则但意图不明确（如「把那个删了」没有对象）。

输出：一句具体问题 + 2–3 个候选，例如：

> 你是想（A）分析这个 CSV 并出图，还是（B）只读取前几行看看？

不设固定的澄清率目标；看**澄清后的任务成功率**。

---

## 8. 回退白名单

- `data.analysis` → `code.execute`（分析脚本可以直接跑）
- `code.execute` → `general.chat`（解释报错）
- `file.operate` → `code.execute`
- `platform.inspect` 不降级（只读，失败就报错）

回退**不降低审批等级**：高风险回退仍要审批。

---

## 9. 轨迹事件

事件类型 `route_decided`，字段：

```json
{
  "event": "route_decided",
  "decision": "route",
  "intent": "data.analysis",
  "confidence": 0.86,
  "risk": "low",
  "requires_approval": false,
  "capability": "sandbox_exec",
  "execution_type": "local_sandbox",
  "model_policy": "cheap",
  "candidates": [["data.analysis", 0.86], ["code.execute", 0.41]],
  "reason": "rule:analysis_kw",
  "source": "rule"
}
```

事件顺序（run_started 由 Agent 先写）：

```
run_started → route_decided → approval_checked → exec_queued → node_selected
→ exec_started → exec_succeeded → artifact → usage/cost
```

---

## 10. 接入点（现有代码）

| 位置 | 作用 |
|---|---|
| `src/chat_secure_api.py` | 聊天入口，请求准入与流式输出 |
| `src/agent.py::chat`（L1095） | Agent 主循环入口；在此调用 Router |
| `src/agent.py::execute_tool_on_platform`（L704） | 工具 → 平台 exec 的桥 |
| `src/core/tool_registry.py` | 工具白名单与策略 |
| `src/platform_api.py` | 平台准入、队列、节点调度 |

Router 在 `chat()` 开头调用一次，产出 `RouteDecision` 并落 `route_decided`；后续工具调用按 `allowed_tools` 过滤。

---

## 11. 评估

- 数据集：5 类 + 高危子集；去重后切 train/dev/test；test 冻结并记录哈希。
- 指标：总体准确率、macro-F1、各类 P/R、高危漏审批（必须 0）、澄清后成功率、路由延迟（分规则/分类器/冷加载）。
- 评估对象：`Router.decide()` 的完整输出，不是单独的模型。

---

## 12. 明确不做

- 不在 Router 里查数据库/队列/节点状态（会过期）。
- 不用 LLM 做最终权限判断。
- 不把 `clarify` 当 intent 统计。
- 不引入独立服务或新仓库。
