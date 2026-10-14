# 认证、越权与额度隔离说明

本文说明 Personal Agent 的身份体系、越权防护与额度原子性。
其中每一条都能在代码里核对，文末另附"现状边界"与"不要讲错的说法"。

一句话概括：**身份只来自服务端会话表，权限过滤下推到 SQL，
额度用整数纳元 + 事务预留。**

代码位置：`src/auth_api.py`、`src/core/auth_store.py`、
`src/core/chat_ownership.py`、`src/core/reminder_store.py`、
`src/core/ai_budget.py`、`src/core/chat_quota.py`。

---

## 0. 先纠正三个常见讲法

项目早期的口头/文档描述里出现过下面这些说法，**都与代码不符**：

| 常见说法 | 代码实际情况 |
| --- | --- |
| "JWT 鉴权" | **不是 JWT**。是不透明随机令牌 `secrets.token_urlsafe(32)`，数据库只存它的 SHA-256 摘要 |
| "httpOnly cookie" | **没有任何 cookie**。凭据走 `Authorization: Bearer <token>`，前端存在 `sessionStorage` |
| "bcrypt / argon2 加密密码" | 是 **PBKDF2-HMAC-SHA256**，`PASSWORD_ROUNDS = 600_000`，每个用户独立 salt |

> 全仓检索 `set_cookie` / `delete_cookie` / `httponly` / `samesite` / `jwt` / `jose`
> 在 `src/` 下**零命中**。这几条在面试里说错，等于把设计讲成了另一个东西。

---

## 1. 身份与角色

### 角色与等级

```python
ROLE_LEVEL = {"member": 0, "trusted": 1, "admin": 2, "owner": 3}
```

权限判断一律走数值比较（`require_role(token, minimum_role)`），
**不是**靠前端隐藏入口。前端隐藏只是体验，真正的门在后端。

### 用户名与密码规则

- 用户名：`^[a-z][a-z0-9_]{2,31}$`，统一转小写（避免 `Alice` / `alice` 双账号）；
- 密码：12–1024 **字节**（按 UTF-8 编码计，不是字符数）。

### 密码存储

```python
hashlib.pbkdf2_hmac("sha256", raw_password, salt, 600_000)   # 每人独立 salt
secrets.compare_digest(candidate_hash, stored_hash)           # 常量时间比较
```

两个细节值得讲：

1. 比较用 `secrets.compare_digest`，避免按字节提前返回带来的时序侧信道；
2. 用户不存在、账号被禁用、密码错误三种情况**返回同一句**
   "用户名或密码不正确"，且都会真的走一次哈希计算 —— 不给出账号存在性的信号。

### 会话

| | 实现 |
| --- | --- |
| 令牌生成 | `secrets.token_urlsafe(32)`（256 位熵） |
| 存储 | 表 `auth_sessions` 主键是 `token_hash` = 令牌的 SHA-256 摘要，**不存明文** |
| 有效期 | 默认 24 小时，入参校验 1–168 小时 |
| 撤销 | `revoked_at` 软撤销，`logout` 写时间戳，立即失效 |
| 校验 | `session_user()` 一次查询同时要求 `revoked_at IS NULL`、`expires_at > now`、`u.active = 1` |

**数据库泄露 ≠ 会话可直接复用**：泄露到的是摘要，攻击者无法由摘要反推令牌。

### 传输与前端存放

- 请求头：`Authorization: Bearer <token>`（FastAPI `HTTPBearer`）；
- 前端：存在 `sessionStorage`，键名 `personal-agent-auth-token`，
  校验失败会清除并回到登录页。

设计取舍：**无 cookie ⇒ 没有 CSRF 面**（浏览器不会自动携带凭据）；
代价是 XSS 可以读到令牌 —— 这条出路是内容安全策略与严格输出转义，
不是"换成 cookie"。

---

## 2. 登录防爆破

表 `auth_login_attempts`，双维度计数：

| 维度 | 上限 |
| --- | --- |
| 单用户名 | `MAX_USER_ATTEMPTS = 8` |
| 单 IP | `MAX_IP_ATTEMPTS = 40` |

- 计数器用 `INSERT ... ON CONFLICT DO UPDATE attempts = excluded.attempts + 1` 递增，并发下不会漏计；
- **登录成功后只清用户名维度的计数**（IP 维度保留，避免攻击者用一个好账号刷掉 IP 计数）；
- 客户端 IP 取真实连接来源（`request.client.host`），**不信任 `X-Forwarded-For`**
  —— 否则限流形同虚设。

---

## 3. 邀请码

表 `auth_invites`：主键 `token_hash`（同样只存摘要）、`role`、`issued_by`、
`expires_at`、`used_by`、`created_at`。

- 邀请码能授予的角色只有 `member / trusted / admin`（表上有 CHECK 约束），
  **构造不出 `owner` 邀请码**；
- 签发邀请码要求操作者等级 ≥ 2（admin）；
- 注册时同时校验：邀请码未过期、未被使用、**签发者等级仍 ≥ 2**
  （防止把已降级的账号签发的邀请码继续用来提权）。

---

## 4. 越权与 ID 探测的防护

三条贯穿全项目的规则：

1. **`user_id` 只能来自服务端已验证的会话**，绝不接受前端传入；
2. **权限过滤下推到 SQL 的 `WHERE user_id = ?`**，不做"先查出来再在 Python 里判断"；
3. **越权与不存在返回同一个响应**，不给"这个 ID 存在但不属于你"的信号。

### 聊天请求（`src/core/chat_ownership.py`）

```python
read_owned_record(conn, request_id, user_id)      # WHERE request_id = ? AND user_id = ?
update_owned_status(conn, request_id, user_id, …) # 额外要求 status = 'processing'
```

`claim_owned_request()` 里有两处顺序上的讲究：

```python
# 先判断归属，避免跨用户通过摘要差异探测请求信息。
if existing[4] != user_id:
    raise RequestOwnerMismatch("请求不存在或无权访问")
if existing[1] != digest:
    raise RequestMessageConflict(...)
```

- **先判归属、后比摘要**：否则别人可以用摘要是否匹配来推断你的请求内容；
- 领取新请求用 `BEGIN IMMEDIATE`，保留"同一 request_id 只计费一次"的原子性。

迁移前的老记录 `user_id` 为 `NULL`，**任何账号都读不到**（有意为之，不做自动归属）。

### 提醒（`src/core/reminder_store.py`）

- 列表 / 取消 / 改期全部带 `WHERE id = ? AND user_id = ?`；
- 别人的提醒与不存在的提醒抛同一个 `ReminderNotFound`，
  HTTP 层统一映射成 404 "提醒不存在或无权访问"；
- 工具层再加一道：`reminder_identity._require_identity()` 在**没有绑定登录身份时**
  直接 `PermissionError`，绝不回退到旧的不分用户的全站接口。

### 额度

`ai_budget_reservations` 同时有 `user_id` 维度与全站维度两套上限检查（见下节）。

---

## 5. 额度与并发的原子性

### 金额用整数

```python
UNITS = Decimal("1000000000")   # 1 US$ = 10^9 纳元
```

库里存 `*_nano_usd` **整数**，不进浮点 —— 金额累加不会出现 0.1+0.2 这类误差。

### 预留流程（`reserve_budget`）

```
BEGIN IMMEDIATE
  ├─ 已有同 request_id 的预留？→ 直接返回，不重复占额（幂等）
  ├─ 查个人过去 24h 已用（charged 优先，未结算用 reserved）
  ├─ 查全站过去 24h 已用
  ├─ user_used + amount > user_cap  或  global_used + amount > global_cap → 拒绝
  └─ 全部通过才 INSERT 预留（state='reserved'）
```

- **"先算总额再插入"整体在一个 IMMEDIATE 事务里**，避免两个并发请求各自读到旧余额、
  双双通过检查（超支的经典成因）；
- 状态机：`reserved / settled / uncertain / overrun`；
  结算写入 `charged_nano_usd`，未结算的预留按 `reserved_nano_usd` 计入占用
  —— 这也是额度面板读取的口径；
- 表上有 CHECK 约束（`reserved_nano_usd > 0`、`charged_nano_usd >= 0`、
  状态枚举），脏数据写不进去。

### 拒绝码要区分

| 拒绝码 | 含义 |
| --- | --- |
| `daily_quota_exceeded` | 24 小时**请求次数**用完 |
| `concurrency_limit_exceeded` | 并发过多（个人 2 / 全站 4） |
| `user_budget_exceeded` | 24 小时**金额**预算不足 |

次数上限按角色：member 10 / trusted 40 / admin 80 / owner 100；
金额上限按角色：US$0.075 / 0.30 / 0.60 / 0.75（滚动 24 小时窗口）。

---

## 6. 现状边界（诚实版）

这部分建议主动讲，比被追问出来好：

| 边界 | 现状 |
| --- | --- |
| **两套归属列名不统一** | 聊天请求与提醒用 `user_id`；监控任务用 **`owner_id`**。语义相同、命名不同，属于历史差异，读代码时要分清 |
| **隔离覆盖范围** | 聊天请求（`chat_ownership`）与提醒（`reminder_store`）按 `user_id` 过滤；监控任务（`monitor_tasks`）按 `owner_id` 过滤 —— `create / list / pause / resume / archive` **五处都带归属条件** |
| **监控任务没有入口** | `create_monitor_task()` 的生产调用方为 **0**（唯一调用方是测试），也没有 HTTP 或工具入口。归属逻辑存在，但从用户视角**不可达**，端到端隔离尚未被真实使用验证 —— 这是当前最该补的一块 |
| **外发通知的归属** | `notification_outbox` 原先没有任何归属列（已补 `owner_id`：非破坏性迁移 + 写入时从任务带出；旧记录保持 NULL，不自动归属）。但**按人投递尚未实现**：现在只有一个全局 `NTFY_TOPIC`，实际只能发给站长 |
| 存储 | SQLite 单机；无读写分离、无连接池 |
| 会话 | 无刷新令牌、无轮换、无多设备管理（只有 24h 过期 + 软撤销） |
| 限流 | 计数表在同一个 SQLite 里，单进程有效；多实例部署需换成共享存储 |
| 令牌存放 | `sessionStorage`，XSS 可读；靠 CSP 与输出转义缓解 |
| 旧数据 | 迁移前 `user_id IS NULL` 的聊天记录任何人都读不到；`notification_outbox.owner_id IS NULL` 的旧通知是"未归属"。两者都没有做归属回填 |

另外提醒一个容易记混的模块：`src/core/access_policy.py` **不是权限策略表**，
它决定的是监控/外发遇到 HTTP 状态码后的动作（429 → 暂停、401/402/403 → 停止、
5xx → 有限重试）。名字容易误导。

---

## 7. 面试问答

**Q：为什么不用 JWT？**
A：这里只需要"服务端能撤销的会话"。随机令牌 + 会话表天然支持立即撤销（`revoked_at`），
而 JWT 要撤销就得额外维护黑名单，等于把会话表又加回来。代价是每次请求多一次
SQLite 查询，这个规模完全可接受。

**Q：令牌为什么存摘要？**
A：数据库泄露时，攻击者拿到的是 SHA-256 摘要，无法反推可用令牌。

**Q：怎么防越权与 ID 探测？**
A：`user_id` 只来自会话；过滤写进 SQL 的 `WHERE`；越权与不存在返回同一个 404；
聊天请求里还有一处"先判归属再比摘要"，防止用摘要差异探测别人的请求。

**Q：并发下怎么保证不超支？**
A：金额存整数纳元；"读已用 + 判上限 + 写预留"整体放在 `BEGIN IMMEDIATE`
事务里；表上加 CHECK 约束兜底。

**Q：登录接口怎么防爆破？**
A：按用户名 8 次、按 IP 40 次双维度计数，登录成功只清用户名维度，
IP 取真实连接来源而不信任 `X-Forwarded-For`。

**Q：前端隐藏管理员入口算权限控制吗？**
A：不算。真正的门是后端 `require_role` + 角色等级比较；前端隐藏只是隐藏。

---

## 8. 自查：不要讲错的说法

- ❌ "JWT + httpOnly cookie" → ✅ 不透明随机令牌 + `Authorization: Bearer`
- ❌ "bcrypt 加密密码" → ✅ PBKDF2-HMAC-SHA256，60 万轮，per-user salt
- ❌ "监控任务没有做归属" → ✅ `monitor_tasks` 有 `owner_id`，五个操作都按它过滤；
  **真正缺的是入口**（`create_monitor_task` 只有测试在调），别把"没入口"说成"没隔离"
- ❌ "全项目只用了 user_id 一种归属" → ✅ 监控层用的是 `owner_id`，列名不统一是历史差异
- ❌ "access_policy 是权限策略" → ✅ 它是监控外发的 HTTP 重试策略
- ❌ "管理员看得到所有人对话" → ✅ 接口层没有读取他人对话的入口：
  `/chat/requests/{id}` 走 `read_owned_record()`，一律按归属过滤，
  管理员身份也不能绕过（用量审计与内容访问权分离）
