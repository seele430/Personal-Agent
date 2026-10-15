# Agent 工具调用层设计说明

本文说明 Personal Agent 里"模型调用工具"这一层的完整结构：工具怎么定义、
模型怎么选、服务端怎么否决、结果怎么回填、怎么验证没有退化。

一句话概括：**选择权给模型，否决权在服务端，效果用离线 eval 量化。**

代码位置：`src/agent.py`（协议与执行）、`src/core/reminder_identity.py`
（身份绑定入口）、`src/core/reminder_store.py`（数据层）、
`src/evals/reminder_eval.py` + `scripts/run_reminder_eval.py`（eval）。

> 术语澄清：这里是 **DeepSeek 原生 Function Calling**，不是文本 ReAct。
> 代码里没有 `Thought` / `Action` / `Action Input` / `Observation` 这类字符串。
> 手写 ReAct 控制流是另一个项目（DeepResearch），两者不要混讲。

---

## 1. 三段式结构

### 第 1 段 · 工具定义（function schema）

`src/agent.py` 顶部定义四个工具，汇总为 `REMINDER_TOOLS`：

| 工具 | 作用 | 关键参数 |
| --- | --- | --- |
| `create_reminder` | 创建提醒 | `message`、`minutes`（相对当前本地时间的整数分钟） |
| `list_reminders` | 列出自己的提醒（含编号） | 无 |
| `cancel_reminder` | 取消一条待发提醒 | `reminder_id` |
| `update_reminder` | 改内容或改时间 | `reminder_id`、`message`/`minutes` 至少一个 |

每个工具是标准的 OpenAI 兼容结构：

```python
{
    "type": "function",
    "function": {
        "name": "cancel_reminder",
        "description": (
            "取消当前用户的一条尚未发送的提醒。"
            "reminder_id 必须来自 list_reminders 的结果，"
            "或用户明确说出的编号。"
            "没有编号时先询问或先列出，不要猜测。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "integer", "description": "提醒编号"},
            },
            "required": ["reminder_id"],
        },
    },
}
```

设计要点：**`description` 不只写"这个工具做什么"，还要写"什么时候不许用"**
（"不要猜测编号"、"必须来自 list 结果"）。这把一部分决策约束前移到了协议里 ——
模型看得到，但服务端并不依赖它遵守。

### 第 2 段 · 暴露与决策（谁来决定调不调）

`_completion(messages, tool_choice, tools=None)` 是唯一的模型调用出口：

```python
kwargs = {
    "model": MODEL,
    "messages": messages,
    "tools": REMINDER_TOOLS if tools is None else tools,
    "tool_choice": tool_choice,
    "max_tokens": MAX_OUTPUT_TOKENS,
}
```

`chat()` 是**两段式固定形状**，循环有确定上界：

```
用户消息
   │
   ├─ 第 1 次调用  tool_choice="auto"   ← 调不调、调哪个，交给模型
   │       │
   │       ├── 没有 tool_calls → 直接返回自然语言，结束
   │       │
   │       └── 有 tool_calls
   │              ├─ 数量 > MAX_REMINDER_TOOL_CALLS(1) → 整轮拒绝（fail closed）
   │              ├─ 先对全部调用做参数形态与体积校验（_check_tool_call_arguments）
   │              ├─ 逐个执行 _execute_reminder_tool()
   │              └─ 结果以 role="tool" + tool_call_id 回填进 messages
   │
   └─ 第 2 次调用  tool_choice="none"   ← 强制收口，只能用自然语言总结
```

第 2 次的 `tool_choice="none"` 很关键：**它保证了这一轮不会自旋**，
模型不能一边总结一边再发起工具调用。

### 第 3 段 · 执行（`_execute_reminder_tool`）

按函数名分发到四个分支，每个分支都走"经过登录身份校验的入口"。
未知工具名直接拒绝：

```python
if name != "create_reminder":
    raise ValueError("未知工具")
```

---

## 2. 工具选择：两层决策

### 第 1 层：模型侧（软约束）

靠 schema 的 `description` + `build_system_prompt()` 共同约束：

| 工具 | 该调用的场景 | 不该调用的场景 |
| --- | --- | --- |
| `create_reminder` | 用户要提醒**且时间明确**（"10 分钟后…"、"今晚 8 点…"） | 只有日期/时段没有钟点（"下周一早上"）、时间已过去、没给时间 → **先问，不许猜** |
| `list_reminders` | "我有哪些提醒" | 与提醒无关的闲聊 |
| `cancel_reminder` | 有明确编号（用户说的，或上一步列出来的） | **没编号 → 先问或先列，不许猜** |
| `update_reminder` | 同上，且至少给 `message` 或 `minutes` 之一 | 目标提醒已发送/已取消 |

system prompt 里写死的规则（`build_system_prompt`）：

- 具体钟点（今晚 8 点、明天 9 点）按当前本地时间换算成整数分钟，并在回复里说明换算结果；
- 只有日期或时段、或时间已经过去 → 先询问，不要猜测；
- 工具返回失败必须明确告知用户；
- **只有工具返回成功，才能说"提醒已保存"**；
- 提醒已保存 ≠ 通知已经送达。
- **知识库引用（仅当注入了检索片段）**：用 `[n]` 标注依据，只引用真实出现过的编号；
  检索为空时不注入片段、也不下"要求引用"的指令。

### 第 2 层：服务端侧（硬约束，不信模型）

这一层才是安全边界所在，共六条：

1. **执行前先全量校验**：`_check_tool_call_arguments()` 在**任何副作用之前**
   对全部 `tool_calls` 跑完，检查参数是字符串且不超过
   `MAX_TOOL_ARGUMENT_BYTES = 4096` 字节。
2. **单轮调用次数上限**：`MAX_REMINDER_TOOL_CALLS = 1`，超出直接整轮 `RuntimeError`，
   不给"部分执行"的机会。
3. **工具名白名单**：只认四个名字，模型幻觉出来的工具名不会被执行。
4. **参数语义校验**（与 `create_reminder` 共用同一套，上限不会漂移）：
   - `_clean_reminder_message`：去空白，UTF-8 编码不超过 1024 字节；
   - `_clean_reminder_minutes`：1–43200 的整数，**显式拒绝 `bool`**
     （Python 里 `True` 是 `int` 的子类，`type(x) is not int` 才能挡住）；
   - `_require_reminder_id`：正整数。
5. **身份是前提**：只有 `bind_authenticated_reminder()` 绑定过登录身份才允许读写，
   没有身份一律 `PermissionError`，且**绝不回退到旧的不分用户的全站创建接口**。
6. **归属校验下推到 SQL**：所有查询都带 `WHERE user_id = ?`；
   别人的记录与不存在的记录返回**同一个** 404，避免用 ID 探测他人数据。

`list_reminders` 另加两条：输出条数上限 `MAX_LISTED_REMINDERS = 20`，
且只返回 `id / message / remind_at / status`，不返回 `user_id`。

---

## 3. 可观测：每次调用留下痕迹

每次模型调用都会写入 `latency_ms` / `finish_reason` / `tool_names`：

- `tool_names` **只记录工具名，不记录参数** —— 回溯"这次为什么慢/为什么贵"够用，
  同时避免把用户内容落进用量库；
- trace 字段在响应校验**之前**提取，所以异常结束的调用也留得下记录。

---

## 4. 效果验证：离线 eval

`evals/reminder/cases.jsonl` 共 32 条用例，8 个类别（相对时间、绝对时间、
含糊表达、缺时间、过去时间、无意图、其它意图、注入防护）。

- 默认**干跑**：只加载并校验用例，不联网、不花钱；
- 加 `--confirm-cost` 才真正调用模型，并按 `--max-cost` 预估上限拦截；
- 判定四档：

| expect | 含义 |
| --- | --- |
| `call` | 必须调用 1 次，且 `minutes` 在容差内、内容命中关键词 |
| `no_call` | 一次工具都不许调 |
| `at_most_one` | 最多 1 次，且参数合法（用于注入与"一次说两个提醒"） |
| `no_mutation` | **只读工具放行，任何副作用失败** |

`no_mutation` 是加进 `list/cancel/update` 三个工具之后补的：以前只有
"完全不许调工具"一种表达，无法区分"读一下可以"和"改数据不行"。
`MUTATING_TOOL_NAMES` 明确列出 `create/cancel/update` 三个会产生副作用的工具。

运行方式：

```powershell
python scripts/run_reminder_eval.py                  # 干跑
python scripts/run_reminder_eval.py --confirm-cost   # 真实调用，约 US$0.004
```

结果以 JSON 归档到 `evals/reminder/results/`，并自动与上一次对比，
列出**退步**和**修复**的用例 id。

一条真实经验：给模型加上 `list/cancel/update` 三个工具后，eval 从 32/32 掉到
30/32。查明原因不是行为退化 —— `chat_04`（"我现在有哪些提醒？"）模型调
`list_reminders` 本来就是正确行为，是**判定的表达力不够**。补上 `no_mutation`
之后实时复跑回到 32/32。这类"先判断是产品错了还是评测错了"的分诊，
比让测试变绿更重要。

---

## 5. 已知取舍（面试常被追问）

| 取舍 | 现状 | 原因 / 代价 |
| --- | --- | --- |
| 单轮只允许 1 次工具调用 | `MAX_REMINDER_TOOL_CALLS = 1` | 一次对话产生的副作用数量必须可预期；代价是复杂指令要分两轮说 |
| 取消/改期必须给显式编号 | 不做"按内容模糊匹配" | 猜错一个字的代价是删错提醒，宁可要编号 |
| 聊天无对话历史 | `chat(user_input)` 单轮无状态 | "先列后删"必然跨两轮：模型列出 → 用户报编号 → 模型删 |
| 站内提醒只在网页可见 | 手机推送目前只对站长的 ntfy 有效 | 属于产品阶段的取舍，不是实现缺陷 |

---

## 6. 与 DeepResearch 的区别（避免讲错）

| | Personal Agent（本项目） | DeepResearch |
| --- | --- | --- |
| 工具协议 | DeepSeek 原生 Function Calling | 同为原生 Function Calling（另有 `REACT_MODE=text` 的文本 ReAct 对照实现） |
| 循环形态 | **单轮工具 + 一轮收口**（两次调用，上界固定） | **手写 ReAct 主循环**，多轮「推理 → 调用 → 观察」 |
| 约束重点 | 身份隔离、副作用上限、单轮次数 | 调用去重、调用次数上限（5 次搜索 / 8 次阅读）、抓取降级 |

一句话区分：**这里讲"工具协议 + 服务端否决 + eval 量化"，
那里讲"手写循环控制流 + 去重与上限"。**
