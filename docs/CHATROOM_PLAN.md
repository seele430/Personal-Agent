# 聊天室（多 Bot 讨论）设计

> 对应 `DEVELOPMENT_PLAN.md` 的 **M5（Bot Studio）+ M6 后半（BYOK）+ M8（多 Bot 讨论协调器）**。
> 本文把「用户一句话 → 多 Bot 各自表态 → 主持人汇总成决策 → 用户选择 → 默认 Bot 执行工具」这条闭环固化下来。
> 状态：设计 v1（2026-10-16），随开发迭代修订。默认取值均可调，先做、发现问题再改。

---

## 0. 一句话

让用户把多个「带不同天然立场的 Bot」拉进同一个聊天室，各说一句核心观点；
分歧由独立的**主持人**汇总成决策选项，用户选一个，再由**用户的默认 Bot**去执行工具。
全程讨论无副作用，副作用只发生在「用户确认后」这一步。

## 1. 角色模型

| 角色 | 来源 | API 计费 | 说明 |
|---|---|---|---|
| **站长默认 Bot** | 平台预置 | 平台配额 | 初始也是每个用户的「默认 Bot」 |
| **用户个人 Bot** | ① 用户自带 key（BYOK）② 平台 API | ① 用户自己的账 ② 平台配额 | 单用户最多 3 个 |
| **主持人** | 平台预置 | 平台配额 | **独立角色，不参与讨论**，只在讨论结束后做汇总，避免汇总偏向自己 |

- **用户的默认 Bot**：每个用户有 `default_bot_id`（初始 = 站长 Bot）。讨论出的决策，由它来执行工具。
- **工具执行权限**：只有「用户的默认 Bot」能真调工具，且仅在用户选择决策之后；讨论中的其他 Bot 只「说」不「做」。

## 2. 身份卡（identity card）—— Bot 差异化的关键

Bot 的差异化靠**提示词工程**，而不是靠模型本身：即使是同一个 API、同一个模型，
不同的身份卡也会产出不同立场，避免发言完全重合。

| 字段 | 说明 |
|---|---|
| `name` | 对外显示名 |
| `persona` | 人设/天然立场（如「保守派」「激进派」「风险厌恶者」「乐观主义者」）——差异化的核心 |
| `system_prompt` | 行为指令（如何在 persona 立场下发言、如何遵守发言纪律） |
| `avatar` | 头像风格标识（`deepseek`/`doubao`/`qwen`/`kimi`/`ernie`/`claude`/`gpt`/`gemini`/`default`），前端映射到风格化图标，**不用厂商官方 logo 原图（商标版权）** |
| `expertise` | 可选，领域标签（成本/技术/合规…） |
| `style` | 可选，语气（简洁/数据控/爱打比方…） |

**原则**：身份卡是「天然立场」的载体，不是「人格分裂」的开关。两个 Bot 若 persona 相同，发言大概率重合；
所以创建 Bot 时，身份卡应鼓励用户选/写不同的立场。

## 3. 发言机制（一轮讨论的状态机）

```
用户发一句话
   ↓
每个 Bot 各说一句「核心观点」（限字数，默认 80 字）
   ↓
[举手机制] Bot 想展开说明 → 举手 → 用户同意 → 展开
   ↓
[插话机制] Bot 展开时，其他 Bot 想追问质疑 → 举手 → 用户同意 → 原 Bot 停止、优先应对插话
   ↓
（用户随时可以结束讨论，触发主持人汇总）
```

### 3.1 核心观点（每 Bot 一句）
- 用户说一句，**每个 Bot 允许说一句**（且只有一句）。
- 这一句**限字数**（默认 80 字），逼 Bot 只给最核心的观点，不灌水。
- 多 Bot **并发**生成这一句（互不看见），保证观点独立、不被带偏。

### 3.0 @ 点名（定向发言）
- 输入框打出 `@` → 前端弹出该聊天室可 @ 的 Bot 列表（复用 S2 的 `list_bots`）。
- 点击某个 Bot → 输入框插入 `@bot名`（交互类似 QQ 群 @某人）。
- 发送后：**被 @ 的 Bot 单独发言**，未被 @ 的 Bot 不发言。
- 未 @ 任何 Bot → 回到 §3.1 的「所有 Bot 各说一句」。
- @ 匹配按 Bot 名（聊天室内唯一）；一次是否允许 @ 多个 Bot 待定。

### 3.2 举手机制（展开需要用户同意）
- Bot 说完核心观点后，若想**展开说明**，必须先「举手」（提交一个展开请求），等用户同意后才输出。
- 举手时 Bot 说明「我要展开什么」，用户判断是否值得展开。
- 这是**人机闭环的限流阀**：展开是额外一次调用，用户不点头就不烧钱、也不产生噪声。

### 3.3 插话机制（追问质疑需用户同意）
- Bot 展开说明时，其他 Bot 想**追问/质疑**，同样必须「举手」，用户同意后才允许插话。
- 允许插话时，**原 Bot 停止输出，优先应对插话**。
- 插话也要限字数（默认同 80 字），只允许一个核心质疑。

### 3.4 活跃度（惩罚无脑反驳）
- 每个 Bot 在聊天室里有一个**活跃度分**（`activity`，默认 100）。
- 举手是否合理，由**用户判断**：追问/反驳不合理 → 扣活跃度（默认 −20）。
- 活跃度低的后果：举手冷却变长（活跃度 < 40 时，举手频率减半），甚至临时禁止举手。
- 目的：让「无脑反驳」有代价，鼓励高质量参与，而不是靠 Bot 自觉。

## 4. 汇总与决策（主持人）

讨论结束后（用户触发），主持人做一次汇总调用，输出**结构化**结果：

- **共识**：多数 Bot 一致的点；
- **分歧**：立场冲突的点，各带各自理由；
- **决策选项**：2–3 个可执行选项，每个附「代价 / 风险」；
- **噪声过滤**：去重（多个 Bot 说同一件事）+ 忽略跑题/低质量内容——这一步做噪声处理，讨论中不硬拦。

用户从决策选项里**选一个** → 用户的默认 Bot 把该选项转成**真实工具调用**（走现有工具白名单 + 单 Bot 权限表）执行。

## 5. 记忆边界

| 层级 | 存什么 | 归属 |
|---|---|---|
| 讨论记录 | 聊天室完整消息流 | `room_id`（聊天室共享，§2.3 的 room 维度） |
| Bot 长期记忆 | 私聊 + 用户确认的内容 | `bot_id`（M7 已预留 `knowledge_documents.bot_id`） |

- **讨论全文不自动灌进每个 Bot 的长期记忆**——否则 Bot 会把别人的观点当成自己的，越聊越偏。
- 用户想让某个观点进入某 Bot 记忆，需显式确认（或来自私聊）。

## 6. 成本控制

| 环节 | 调用次数 | 说明 |
|---|---|---|
| 核心观点 | 1 次 / Bot（并发） | 单轮，限字数 |
| 展开 | 1 次 / 次举手 | 用户同意才发生 |
| 插话 | 1 次 / 次举手 | 用户同意才发生 |
| 汇总 | 1 次（主持人） | 结束讨论时 |
| 执行 | 1 次（默认 Bot） | 用户选决策后 |

- 一次「用户一句话」的最低成本 = 1 次汇总前的 N 个 Bot 各一句 + （可选）举手。
- 熔断沿用 §2.2：单次讨论总预算、每 Bot 输出上限、并发数。
- **BYOK 分摊**：谁发起讨论谁承担主持人那次调用的成本；每个 Bot 自己的发言记到各自账号（BYOK bot 记用户 key 用量，平台 bot 记平台配额）。

## 7. 数据模型（SQLite 起步，沿用 M7 的「先不上 PostgreSQL」结论）

```sql
bots (
    id INTEGER PRIMARY KEY,
    owner_id TEXT,               -- NULL = 站长/平台预置
    name TEXT NOT NULL,
    persona TEXT NOT NULL,       -- 身份卡：天然立场
    system_prompt TEXT NOT NULL, -- 身份卡：行为指令
    expertise TEXT,              -- 身份卡：可选
    style TEXT,                  -- 身份卡：可选
    api_source TEXT NOT NULL     -- 'platform' | 'byok'
        CHECK (api_source IN ('platform','byok')),
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
)

rooms (
    id INTEGER PRIMARY KEY,
    owner_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
)

room_bots (
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    activity INTEGER NOT NULL DEFAULT 100,   -- 活跃度
    joined_at TEXT NOT NULL,
    PRIMARY KEY (room_id, bot_id)
)

messages (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,        -- 一轮讨论的 id，汇总/举手都挂它
    speaker TEXT NOT NULL,        -- 'user' | 'bot' | 'host'
    bot_id INTEGER,               -- speaker='bot'/'host' 时非空
    role TEXT NOT NULL,           -- 'core' | 'expand' | 'object' | 'summary' | 'decision'
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
)

hands (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    kind TEXT NOT NULL            -- 'expand' | 'object'
        CHECK (kind IN ('expand','object')),
    reason TEXT NOT NULL,         -- 举手理由（供用户判断合理性）
    status TEXT NOT NULL DEFAULT 'pending'  -- 'pending' | 'approved' | 'rejected'
        CHECK (status IN ('pending','approved','rejected')),
    created_at TEXT NOT NULL
)

-- 用户默认 Bot：users 表加 default_bot_id（初始 = 站长 Bot）
```

## 8. 落地顺序（每个切片可独立验收，不推倒重来）

| 切片 | 内容 | 验收 |
|---|---|---|
| S1 | Bot 实体：`bots` 表 + 身份卡 + store CRUD | 建/查/改/禁用 Bot；单用户上限 3；owner 隔离 |
| S2 | Bot API：创建（BYOK/平台）+ 列表 + 默认 Bot | API 建 Bot、设默认 Bot、越权校验 |
| S3 | 单人聊天：用户选 Bot 私聊，接 `bot_id` 记忆 | 私聊走 Bot 身份卡 + 独立记忆 |
| S4 | 聊天室：`rooms`/`room_bots`/`messages` + 单轮核心观点（并发 + 限字数） | 拉多个 Bot 进房，各说一句核心观点 |
| S5 | 举手/插话/活跃度：`hands` 表 + 状态机 + 扣分 | 展开需同意、插话需同意、不合理扣活跃度 |
| S6 | 主持人汇总 + 决策 + HITL 执行 | 汇总出决策选项，用户选后默认 Bot 执行工具 |

**后端 S1–S6 已完成**（591 测试全绿）；**前端**已接 Bot 管理，聊天室界面待做。

---

## 9. 前端交互待办

### 9.1 已完成
- Bot 管理页 `frontend/src/BotsView.tsx`：创建（身份卡五字段 + 头像）、列表、禁用/启用、设为默认。
- 后端补端点：`GET /bots/default`、`POST /bots/{bot_id}/default`。

### 9.2 待改（2026-10-16 用户反馈，**已记录，未动手**）

**① 不要强制必填 —— 留空也要能创建。**
现状：创建表单三项（名字 / 天然立场 / 行为指令）任一为空即报「都不能为空」，把用户拦在门外（用户原话：*用户就是懒的输入*）。
要求：**留空也允许创建**；缺失字段用默认值兜底（例如名字给「新 Bot」、立场/指令取预设之一），不要让必填校验成为创建门槛。

**② 立场 / 行为指令改成「预设列表点选」。**
要求：点击「天然立场」输入框时，在其**上方弹出预设立场列表**；点击「行为指令」输入框时，同样在上方弹出预设指令列表。默认提供若干**差异化**的立场与指令（保守派 / 激进派 / 风险厌恶者 / 乐观主义者…），点一下填入，仍可手改。
目的：让不同 Bot 有天然差异，避免「同一个 API 出来的机器人发言完全重合」。

### 9.3 待做
- 聊天室界面：房间列表 / 发言流（含 @ 下拉）/ 举手按钮（批准 / 拒绝）/ 汇总决策面板。
