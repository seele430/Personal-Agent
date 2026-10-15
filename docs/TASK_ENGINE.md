# 任务引擎：Source → Condition → Action

本文说明监控任务引擎的分层、不变式与扩展方式。

一句话：**调度与数据源解耦，判断与动作同事务，状态全在 SQLite。**

代码位置：`src/core/source_registry.py`（数据源注册表）、
`src/core/monitor_dispatcher.py`（调度、租约、退避、频率）、
`src/core/task_engine.py`（条目判断与通知落库）、
`src/monitors/*`（各数据源实现）、`src/monitors/http_fetch.py`（共用抓取路径）。

---

## 1. 三层划分

| 概念 | 职责 | 实现位置 | 数据 |
| --- | --- | --- | --- |
| **Source** | 取数据，产出"条目" | `SOURCE_HANDLERS[source_type]` | `monitor_tasks.task_type` 决定用哪个 handler |
| **Condition** | 判断哪些条目是"新的" | `detect_new_items()` 前半段 | `monitor_state`（是否已建基线）+ `seen_items`（见过的条目） |
| **Action** | 把新条目变成待发通知 | `detect_new_items()` 后半段 | `notification_outbox` |

调度本身（何时跑、谁在跑、失败了怎么退避、同一主机多久抓一次）完全在
`monitor_dispatcher` 里，**三层都不关心**。

## 2. 数据源 handler 契约

```python
def handler(monitor_id: str, source_url: str) -> int:
    """返回新增条目数。"""
```

handler 内部的责任链是固定的：

```python
条目 = 抓取并标准化(source_url)          # 各源自己实现
新增 = detect_new_items(monitor_id, 条目)  # 引擎提供，含判断与落库
return len(新增)
```

`detect_new_items` 接受的"条目"是一个列表，每项至少要有 `id`
（业务唯一键）。它返回真正新增的那些条目，并已把它们写进通知队列。

新增一种数据源 = **写一个 handler + 在 `SOURCE_HANDLERS` 里注册一行**，
其余全部复用：租约、退避、频率限制、执行历史、通知队列、去重、基线。

## 3. 为什么 Condition 与 Action 必须同事务

这是整个引擎最关键的一处设计，也是"恰好一次"能成立的原因：

```python
with closing(sqlite3.connect(...)) as conn, conn:
    conn.execute("BEGIN IMMEDIATE")
    ...
    cursor = conn.execute(
        "INSERT OR IGNORE INTO seen_items (monitor_id, item_id, ...) VALUES (?, ?, ...)"
    )
    if initialized and cursor.rowcount == 1:      # 这一条真的是新的
        conn.execute("INSERT OR IGNORE INTO notification_outbox (...) VALUES (...)")
```

如果拆成"先判断、再另开事务写通知"，就会出现两个经典故障：

| 拆开后的做法 | 后果 |
| --- | --- |
| 先记 `seen_items`，后写 outbox | 两步之间崩溃 → 条目被记为"见过"，**通知永远丢失** |
| 先写 outbox，后记 `seen_items` | 两步之间崩溃 → 下次扫描又认为它是新的，**重复通知** |

放在一个 `BEGIN IMMEDIATE` 里，"判断"与"动作"要么都发生、要么都不发生。
所以：**分层是概念上的，事务边界不能按层切。**

## 4. 不变式（都有测试）

| 不变式 | 机制 | 测试 |
| --- | --- | --- |
| 首次检查只建基线，不通知历史条目 | `monitor_state` 没有记录时不写 outbox，只标记已初始化 | `test_first_run_only_builds_baseline` |
| 新增条目**恰好通知一次** | `seen_items` 的主键 `(monitor_id, item_id)` + `INSERT OR IGNORE` 的 `rowcount` 判定 | `test_new_item_notifies_exactly_once` |
| 重启不重放 | 状态只在 SQLite，没有进程内缓存 | `test_state_lives_in_sqlite_so_restart_does_not_replay` |
| 不同任务基线互不干扰 | 去重键带 `monitor_id` | `test_two_monitors_do_not_share_baseline` |
| 一条任务同一时刻只有一个执行者 | `monitor_tasks.lease_token` + `lease_expires_at`，`BEGIN IMMEDIATE` 领取 | `test_monitor_lease.py` |
| 同一主机不会被高频抓取 | 调度层 `reserve_fetch()`（`feed_fetch_log` 表） | `test_same_host_is_deferred_not_failed` |
| 数据源可插拔 | `SOURCE_HANDLERS` 注册表 + `require_source_type()` | `test_dispatcher_routes_by_source_type` |

## 5. 所有数据源共用一条硬化抓取路径

`src/monitors/http_fetch.py` 是**唯一**发出 HTTP 请求的地方：

```
策略校验（协议 / 主机形态 / 全部解析结果）
  → 连接固定到已验证的 IP（消除 DNS TOCTOU）
  → 逐跳复检重定向（公网 302 到内网也拦得住）
  → 响应体积上限（声明值 + 实际读取量）
```

理由很直接：如果每种数据源各写一套 `requests` 调用，SSRF 防护就会有
多个实现、多个可能写错的地方。**新增数据源不新增网络出口** ——
handler 只能通过 `fetch_bytes()` 取数据。

## 6. 现有数据源

| source_type | 取什么 | 条目 id | 说明 |
| --- | --- | --- | --- |
| `rss` | RSS / Atom feed | 条目自身的 id 或 link | feedparser 解析 |
| `http_text` | 任意公网 HTTPS 页面 | 正文归一化后的 sha256 | 内容变了才通知 |

`http_text` 的意义在于**验证抽象成立**：它复用了完全相同的
"基线 + 去重 + 恰好一次"语义，判断与动作那一层一行都没改。

## 7. 与 M2 验收标准对照

| `DEVELOPMENT_PLAN` M2 验收 | 现状 |
| --- | --- |
| SQLite 存任务 / 基线 / 状态 | ✅ `monitor_tasks` / `monitor_state` / `seen_items` |
| 首次不通知旧文章 | ✅ 有测试 |
| 新增文章恰好通知一次 | ✅ 有测试（含重复扫描） |
| 重启恢复 | ✅ 状态全在 SQLite，无进程内缓存 |

## 8. 投递：谁收到、在哪里确认

通知进 `notification_outbox` 时带上任务主人（`owner_id`），
**投递时**再按主人的角色决定渠道：

| 任务主人 | 渠道 | 说明 |
| --- | --- | --- |
| 站长 | `ntfy` | 沿用唯一的全局 topic |
| 其他角色 | `in_app` | 站内，本人读取并确认 |
| 用户不存在 / 角色读不到 | `in_app` | 宁可在站内没人看，也不误推别人手机 |
| 历史行（没有归属） | `ntfy` | 迁移前的行本就发全局，保持原行为 |

worker 只搬运 `ntfy` 那一类；其余翻成 `status='in_app'` 后**不碰网络**。

用户在**统一收件箱**里确认（`src/core/inbox.py` + `src/inbox_api.py`）：

- `GET /inbox` 聚合两类待确认消息 —— 监控通知（`status='in_app'`）
  与**到点的站内提醒**（`delivery='in_app'` 且 `status='pending'` 且已到期）；
- `POST /inbox/{kind}/{item_id}/ack` 按 `kind` 路由到对应来源
  （监控置 `read`，提醒置 `sent`）。

这里只做**读取聚合 + 确认路由**，没有把两张表合并：提醒表的状态机与到期
语义和通知表差别很大，合并要改动已经稳定的提醒调度与提醒面板，收益不抵风险。
两条链路都稳定运行一段时间后，再考虑真正的合并。

## 9. 已知边界（诚实版）

- **是"拉取 + 内容变化"模型，不是通用工作流引擎**：没有条件表达式 DSL、
  没有分支与串联、没有依赖编排。要变成通用引擎，得先把 Condition 抽成
  可配置的判定函数，再把 Action 抽成可组合的动作。
- **手机推送仍然只有站长有**：非站长用户走站内（打开网页才看得到）。
  要给所有人手机推送，需要每人一个 ntfy topic（公开 topic 名可被他人
  订阅，必须高熵）或自建带认证的推送服务 —— 这是产品决策，不是实现缺口。
- **创建任务开放给站长与管理员**（普通成员不行）：每个任务都是一次计划内
  的对外抓取，开放给全体成员等于把服务器当抓取代理用。这个口径与"按人
  投递"绑定 —— 投递能力落地之前创建是收紧的，落地之后才放宽
  （`src/monitors_api.py` 顶部注释记录了这次演变）。
- **`http_text` 有噪音**：只做空白归一化，页面里的时间戳、广告位、
  轮播切换都会算成"内容变了"。要做成品级需要按选择器抽取正文。
- **频率限制是每主机 5 分钟**，不是按数据源类型或按用户区分。
- **收件箱上限 50 条**，超出部分不显示（未做分页）。
