# TEST_ENGINEERING_REPORT.md

Personal Agent 测试工程化整理报告

- 生成时间：2026-10-12（验收更新：2026-10-12）
- 项目根目录：`C:\Users\123\Personal Agent`
- 运行环境：`.venv\Scripts\python.exe`（Python 3.12.10）
- 报告口径：**测试用例数（cases）**，不是文件数；文件数单独标注
- 整理范围：扫描、登记、审计、报告；外加**一次**为闭合隔离缺口所必需的测试隔离修复（见 §4.1）
- 未做的事：未修改业务代码（`src/**` 未动）、未删除任何测试、未移动测试文件位置、未关闭安全守卫、未运行未隔离的全量测试作为验收依据
- 修改文件清单见下方表格

> **最终验收结论：PASS。**
> `Test Schema Audit: PASS` / `Full Protected Backend Tests: PASS` / `Ran 253 tests` / `OK`

### 修改文件清单

| 文件 | 类型 | 改动 |
|---|---|---|
| `tests/registry.py` | 改 | Schema 6 → 39 条（100% 覆盖）；新增 `status`、`KNOWN_ISOLATION_GAPS`（当前为空）、`schema_status()` / `expected_status()` / `summarize()` |
| `scripts/run_tests.py` | 改 | 输出补 `Duration` / `Result`；`audit` 命令接入静态隔离审计（分层判定与加载逻辑未动） |
| `scripts/audit_test_isolation.py` | **新增** | 只读静态隔离审计（真实 DB 写入 / 网络出口 / Schema 一致性 / 登记完整性） |
| `tests/test_monitor_lease.py` | 改 | +12 行 / 0 删除：补 `task_engine`、`task_run_history` 的 `DB_PATH` patch，闭合隔离缺口（断言与业务代码未动） |
| `TEST_ENGINEERING_REPORT.md` | **新增** | 本报告 |

---

## 1. 测试总览

最终分层结果（`python scripts/run_tests.py audit`）：

| 层级 | 文件数 | 用例数 | 说明 |
|---|---|---|---|
| Total | 39 | 253 | `tests/**/test_*.py` 全部纳入，无遗漏 |
| Unit (L1) | 5 | 32 | 无数据库、无真实网络 |
| Integration (L2) | 24 | 154 | 临时 SQLite、依赖打桩 |
| API (L3) | 10 | 67 | TestClient 进程内 ASGI、mock 认证、临时库 |
| Legacy | **0** | **0** | 已无未分类测试 |

Registry 侧口径：

| 指标 | 数值 |
|---|---|
| Schema 条目总数 | **39 / 39 个测试文件** |
| 整理前已登记 | 6 |
| 本次补登记（`registered-inferred`） | 33 |
| 仍为 legacy / unregistered | **0** |
| 已知隔离缺口（KNOWN_ISOLATION_GAPS） | **0** |
| 仓库根目录游离测试（不在 `tests/`，运行器不收） | 1（重复文件，见 §4.2） |

> 保留的 6 条原始 Schema 一字未改（`layer/domain/database/authentication/external_network/risk` 全部原样），
> 因此原有分类行为完全不变。

---

## 2. 测试矩阵

`status` 取值：`registered` = 整理前已登记；`registered-inferred` = 本次补登记，元数据由静态源码阅读推断。

<!-- MATRIX -->

| File | Layer | Domain | DB | Network | Auth | Risk | Status |
|---|---|---|---|---|---|---|---|
| **L1 单元测试** | | | | | | | |
| `test_agent_bounds.py` | unit | agent | none | none | none | low | registered-inferred |
| `test_agent_call_count.py` | unit | agent | none | none | none | low | registered-inferred |
| `test_agent_usage.py` | unit | agent | none | none | none | low | registered-inferred |
| `test_rss_errors.py` | unit | rss | none | mocked | none | medium | registered-inferred |
| `unit/test_agent_tool_limits.py` | unit | reminder | none | mocked | none | medium | registered |
| **L2 数据集成测试** | | | | | | | |
| `integration/test_guarded_asyncio.py` | integration | test_infra | none | none | none | low | registered-inferred |
| `integration/test_reminder_store.py` | integration | reminder | temporary_sqlite | none | none | high | registered |
| `integration/test_test_safety.py` | integration | test_infra | temporary_sqlite | none | none | medium | registered-inferred |
| `test_access_policy_dispatcher.py` | integration | access_policy | temporary_sqlite | mocked | none | medium | registered-inferred |
| `test_admin_alerts.py` | integration | alerting | temporary_sqlite | none | none | medium | registered-inferred |
| `test_ai_budget.py` | integration | billing | temporary_sqlite | none | none | high | registered |
| `test_ai_budget_reconcile.py` | integration | billing | temporary_sqlite | none | none | high | registered |
| `test_ai_pricing.py` | integration | billing | temporary_sqlite | none | none | high | registered-inferred |
| `test_ai_usage.py` | integration | billing | temporary_sqlite | none | none | high | registered-inferred |
| `test_api_idempotency.py` | integration | chat | temporary_sqlite | none | none | medium | registered-inferred |
| `test_auth_store.py` | integration | auth | temporary_sqlite | none | mocked | high | registered-inferred |
| `test_bootstrap_owner.py` | integration | auth | temporary_sqlite | none | mocked | high | registered-inferred |
| `test_chat_admission.py` | integration | chat | temporary_sqlite | none | none | medium | registered-inferred |
| `test_chat_ownership.py` | integration | chat | temporary_sqlite | none | none | medium | registered-inferred |
| `test_chat_quota.py` | integration | chat | temporary_sqlite | none | none | medium | registered-inferred |
| `test_dispatcher_history.py` | integration | dispatcher | temporary_sqlite | none | none | medium | registered-inferred |
| `test_monitor_lease.py` | integration | monitor | temporary_sqlite | none | none | medium | registered-inferred |
| `test_outbox.py` | integration | outbox | temporary_sqlite | mocked | none | medium | registered-inferred |
| `test_outbox_429.py` | integration | outbox | temporary_sqlite | mocked | none | medium | registered-inferred |
| `test_outbox_backoff.py` | integration | outbox | temporary_sqlite | none | none | medium | registered-inferred |
| `test_outbox_http.py` | integration | outbox | temporary_sqlite | mocked | none | medium | registered-inferred |
| `test_reminder_identity.py` | integration | reminder | temporary_sqlite | none | none | medium | registered-inferred |
| `test_task_health.py` | integration | task | temporary_sqlite | none | none | medium | registered-inferred |
| `test_task_run_history.py` | integration | task | temporary_sqlite | none | none | medium | registered-inferred |
| **L3 HTTP/API 测试** | | | | | | | |
| `test_api_http.py` | api | chat | temporary_sqlite | mocked | none | medium | registered-inferred |
| `test_auth_api.py` | api | auth | temporary_sqlite | mocked | mocked | high | registered-inferred |
| `test_chat_budget_http.py` | api | billing | temporary_sqlite | mocked | mocked | high | registered-inferred |
| `test_chat_quota_http.py` | api | chat | temporary_sqlite | mocked | mocked | medium | registered-inferred |
| `test_chat_reconciliation_http.py` | api | billing | temporary_sqlite | mocked | mocked | high | registered-inferred |
| `test_chat_secure_api.py` | api | chat | temporary_sqlite | mocked | mocked | high | registered-inferred |
| `test_chat_usage_http.py` | api | chat | temporary_sqlite | mocked | mocked | medium | registered-inferred |
| `test_login_rate_limit.py` | api | auth | temporary_sqlite | mocked | mocked | high | registered-inferred |
| `test_reminder_api.py` | api | reminder | temporary_sqlite | none | mocked | high | registered |
| `test_reminder_chat_http.py` | api | reminder | temporary_sqlite | mocked | mocked | high | registered |
| **未登记** | | | | | | | |
| （无） | | | | | | | |
| `test_outbox.py`（仓库根，不在 `tests/`） | legacy | outbox | temporary_sqlite | mocked | none | medium | unregistered (duplicate) |

### 元数据判定规则（可复核）

| 字段 | 判定依据 |
|---|---|
| `layer` | 所在目录优先（`unit/`、`integration/`、`api/`）；否则按是否使用 TestClient 判 `api`，是否使用 SQLite/临时库判 `integration`，其余判 `unit` |
| `database` | 出现 `TemporaryDirectory/mkdtemp/:memory:` 或 `*_DB_PATH` 被 patch 到临时路径 → `temporary_sqlite`；无数据库符号 → `none` |
| `external_network` | 直接网络调用被 patch/打桩，或使用 TestClient（进程内 ASGI）→ `mocked`；无网络符号 → `none` |
| `authentication` | 出现 token/登录/Authorization 且被 mock → `mocked`；仅出现字样且非认证路径 → `none` |
| `risk` | `billing` / `auth` 域，或触及并发与资金一致性 → `high`；有临时库或打桩网络 → `medium`；纯逻辑无 IO → `low` |

> 这些字段是**元数据**，只描述测试依赖，**不构成系统隔离**。

---

## 3. 已完成工程化部分

| 能力 | 位置 | 状态 |
|---|---|---|
| Test Runner | `scripts/run_tests.py` | 已有，本次补 Duration 输出 |
| Test Schema Registry | `tests/registry.py` | 已有，本次从 6 → 39 条（100% 覆盖） |
| Layer 分类机制 | `scripts/run_tests.py` `classify_file()` | 已有，行为未改 |
| Guarded Test Runner | `scripts/run_guarded_tests.py` | 已有，未改 |
| Runtime Safety（守卫本体） | `scripts/test_safety.py` | 已有，未改 |
| Isolation 静态审计 | `scripts/audit_test_isolation.py` | **本次新增** |
| 统一入口输出 Layer/Files/Cases/Duration/Result | `scripts/run_tests.py` | 本次补齐 |
| `audit` 命令接入隔离审计 | `scripts/run_tests.py` `audit_isolation()` | **本次新增** |
| KNOWN_ISOLATION_GAPS 登记机制 | `tests/registry.py` | **本次新增**（当前为空） |
| 监控租约测试的临时库隔离修复 | `tests/test_monitor_lease.py` | **本次修复**（见 §4.1） |

### 3.1 本次对 `run_tests.py` 的改动（行为兼容）

1. `audit` 输出新增 `Layer / Files / Duration / Result` 行，用例运行结束打印 `Duration` 与 `Result: PASS|FAIL`。
2. `audit` 命令现在会额外调用静态隔离审计并打印结果；**存在隔离违例时退出码为 2，否则仍为 0**。
3. 未改动：分层判定、注册表校验、目录/层级冲突检查、用例加载方式、退出码语义（除审计新增违例分支）。

### 3.2 隔离审计做了什么

`scripts/audit_test_isolation.py`（只读，标准库，不 import 任何测试模块）：

| 检查 | 规则 |
|---|---|
| 数据库 | 源码出现 `chat_requests.db` / `tasks.db` / `auth_users.db` / `reminders.db` → 违例；若该路径解析在临时根下（`TemporaryDirectory` / `mkdtemp` / `self.temp` / `*_DB_PATH` 被 patch 指向临时路径）→ 降级为提示 |
| 网络 | `requests/httpx` 的 HTTP 方法调用、`urlopen`、裸 socket 的 `connect/connect_ex/sendto/sendall` → 违例；同行有 `patch/mock/assertRaises/guarded_test_runtime` 或位于守护块内 → 跳过；`TestClient`（进程内 ASGI）不算真实网络 |
| Schema 一致性 | 已登记 Schema 出现 `database="real"` 或 `external_network="real"` → 违例 |
| 登记完整性 | 位于 `unit/`、`integration/`、`api/` 目录但无 Schema → 提示 |

审计结果：**39 个文件，违例 0，提示 5**（3 条为「数据库名解析在临时根下」，2 条为「测试文件名以 `test_tasks.db` 之类作为临时文件名」；分层目录登记提示已归零）。

### 3.3 隔离审计的已知局限（必须说明）

1. **静态审计 ≠ 运行时沙箱**。它读源码做符号与上下文判断，无法证明运行期一定不触网、不写真实库。
2. 真正的运行期拦截来自 `scripts/test_safety.py` 的守卫（patch `sqlite3.connect` 与 socket），
   且该守卫只覆盖 SQLite 与 Python socket，**不是操作系统级沙箱**（其 docstring 已声明）。
3. `tests/registry.py` 的元数据**不参与**任何强制隔离，只用于分类与报告。
4. 审计的「提示」与「违例」判定是启发式的；本报告中的每一条提示都能在矩阵里对应到具体文件与行号。

---

## 4. 未整理部分

最终状态：**没有未整理的分层测试**，`legacy = 0`。

### 4.1 曾经的唯一缺口：`test_monitor_lease.py`（**已修复并登记为 L2**）

> 修复前确认（用户要求的两点，逐条给证据）：
> **① 是否已使用临时数据库：否。** 修复前它只 patch 了 `monitor_registry.DB_PATH` / `monitor_dispatcher.DB_PATH`，
> 而真正被使用的 `task_run_history.DB_PATH` 未 patch（`from ... import` 各绑定一份引用）。
> **② 是否会访问真实 `tasks.db`：会。** 受保护运行下实际抛出
> `TestSafetyViolation: Test cannot access project database: C:\Users\123\Personal Agent\tasks.db`（栈见下表）。
> 因此本轮做了一次最小修复，而非仅「确认没有问题」。

| 项目 | 内容 |
|---|---|
| 问题 | `setUp` 只替换 `monitor_registry.DB_PATH` 与 `monitor_dispatcher.DB_PATH`；但 `claim_due_tasks()` 会继续调用 `init_dispatcher_db() → init_run_history()`，后者使用 `src/core/task_run_history.py` 的 `DB_PATH`，仍指向项目 `tasks.db` |
| 深层原因 | `monitor_registry` / `monitor_dispatcher` / `task_run_history` 全部使用 `from src.core.task_engine import DB_PATH`。`from ... import` 会把常量**绑定到各模块自己的命名空间**，因此只替换 `task_engine` 或只替换其中两个模块都无效（实测：替换源头后 `task_run_history.DB_PATH is task_engine.DB_PATH == False`） |
| 证据（修复前） | `tests/test_monitor_lease.py:27-28`；`src/core/monitor_dispatcher.py:86`；`src/core/task_run_history.py:26`；`scripts/test_safety.py:125`；运行期栈 `claim_due_tasks → init_dispatcher_db:86 → init_run_history:26 → sqlite3.connect(DB_PATH)` |
| 修复 | `tests/test_monitor_lease.py` 新增 `from src.core import task_engine` / `from src.core import task_run_history`，并在两处 `with patch...` 中同时替换**四个模块**的 `DB_PATH`（`task_engine`、`task_run_history`、`monitor_registry`、`monitor_dispatcher`） |
| 改动规模 | `12 insertions(+)`、**0 删除**（`git diff --stat`）；**未改动任何断言、未改动任何测试方法签名、未改动被测业务代码**（`src/**` 全程未动） |
| 登记 | 修复后登记为 L2 integration（domain=monitor，database=temporary_sqlite，risk=medium） |
| 验收证据 1 | `run_guarded_tests.py integration`：修复前 `2 errors`（`TestSafetyViolation`）→ 修复后 `24 files / 154 cases / OK` |
| 验收证据 2 | 全量受保护后端：`39 files / 253 cases / OK` |
| 验收证据 3（独立证明） | 把项目里的 `tasks.db`、`auth_users.db`、`chat_requests.db`、`reminders.db` **临时替换成非数据库文件（内容 `NOT-A-DATABASE-POISON`）**，再在守卫下跑该测试文件：`Ran 2 tests ... OK`——若它真的打开过任何一个真实库，SQLite 必然报 `file is not a database`。跑完按字节副本还原，四个库 SHA256 与还原前完全一致，无残留备份文件 |
| 副作用核对 | 受保护全量运行（253 用例）前后，`tasks.db` / `auth_users.db` / `chat_requests.db` / `reminders.db` 的 SHA256 与最后写入时间**全部未变** |

> A/B 对照：整理前原始 6 条注册表下，`integration` 只跑 `5 files / 46 cases`（该文件在 legacy 桶，**压根没被跑**）；
> 补登记后它第一次进入分层运行，守卫当场把它拦下。**这正是「登记」这件事的价值**：
> 31 个 flat 测试此前从未进入受守卫的分层运行，其是否写真实库没有任何机制检查过。

### 4.2 `test_outbox.py`（仓库根目录，不在 `tests/`）

| 项目 | 内容 |
|---|---|
| 状态 | 未被运行器收录（`discover_files()` 只扫 `tests/**`），因此不计入 39 |
| 事实 | 与 `tests/test_outbox.py` **字节级相同**：SHA256 均为 `468186DDB06F3DA22238E7E612102CF347E497A5702A6D45285173766E27DF86` |
| 处理方式 | 未移动、未删除。仅作为重复文件记录在案，见 §6 建议 S4 |

### 4.3 未整理项汇总

| 类别 | 数量 | 说明 |
|---|---|---|
| 未登记的分层测试 | 0 | 已 100% 登记 |
| 隔离缺口 | 0 | 唯一一条已修复闭环（§4.1） |
| 重复文件（仓库根，运行器不收） | 1 | `test_outbox.py` |
| 元数据为静态推断、未经运行期验证 | 33 | 全部标 `registered-inferred`，分布在 L1/L2/L3 |
| 前端测试未纳入本 Runner | 1 套 | `frontend/`（Playwright + build），由 `scripts/test.ps1 -Suite frontend` 单独跑 |

---

## 5. 风险列表

| # | 风险 | 等级 | 位置 / 证据 | 影响 |
|---|---|---|---|---|
| R1 | ~~`test_monitor_lease.py` 会初始化真实 `tasks.db`~~ **已闭环** | ~~高~~ → 关闭 | §4.1 验收证据 1/2/3 | 已修复并纳入受守卫回归 |
| R2 | ~~受保护后端全量 FAIL（253 用例中 2 error）~~ **已闭环** | ~~高~~ → 关闭 | `run_guarded_tests.py backend` → `253 cases / OK` | 现可作为放行门槛 |
| R3 | 33 条 Schema 为静态推断，未经运行期验证 | 中 | `tests/registry.py`，`status=registered-inferred` | 元数据可能有个别字段判断偏差（如 `authentication`） |
| R4 | AI 计费与预算一致性测试（billing 域，6 个文件，56 用例） | **高** | `test_ai_budget.py` / `test_ai_budget_reconcile.py` / `test_ai_pricing.py` / `test_ai_usage.py` / `test_chat_budget_http.py` / `test_chat_reconciliation_http.py` | 涉及金额与并发，回归失败代价高 |
| R5 | 认证与登录限流测试（auth 域，5 个文件，42 用例） | **高** | `test_auth_store.py` / `test_auth_api.py` / `test_bootstrap_owner.py` / `test_login_rate_limit.py` / `test_chat_secure_api.py` | 认证链路回归 = 越权风险 |
| R6 | `test_rss_errors.py` 依赖 `requests` 真实库，靠 patch 上层函数阻断网络 | 中 | `tests/test_rss_errors.py:1-11`，全部用例 patch `rss_monitor.fetch_rss` | 若未来改动 patch 目标，会退化为真实外呼 |
| R7 | 守卫只覆盖 SQLite + Python socket，非 OS 沙箱 | 中 | `scripts/test_safety.py:1-14` | 子进程、非 socket 的 IO 不受守卫约束 |
| R8 | 仓库根 `test_outbox.py` 与 `tests/test_outbox.py` 重复 | 低 | SHA256 相同 | 未来改动容易只改一份，产生双份事实 |
| R9 | ~~未登记测试默认进 legacy，legacy 不参与验收门~~ **已闭环** | ~~低~~ → 关闭 | `legacy = 0`；但机制仍在 | 新增 flat 测试若不登记仍会静默落入 legacy，需要流程约束（S2） |
| R10 | 模块级 `DB_PATH` 绑定是**系统性**陷阱 | 中 | `monitor_registry.py:8` / `monitor_dispatcher.py:13` / `task_run_history.py:9` 均为 `from src.core.task_engine import DB_PATH` | 未来新增模块若沿用该写法，测试只 patch 源头会**无效但不报错**（R1 就是这么来的）。建议新增测试一律 patch 实际使用的模块 |

---

## 6. 后续建议

> 全部为增量建议，**不涉及大规模重构**。

| # | 建议 | 优先级 | 代价 | 备注 |
|---|---|---|---|---|
| S1 | ~~修 `test_monitor_lease.py` 并登记 L2~~ **已完成** | — | — | 见 §4.1 |
| S2 | 把「登记后跑一次 `run_guarded_tests.py backend`」固化为登记流程 | 高 | 每次登记约 44s | 用于拦住类似 R1 的新缺口；当前全量已绿，门槛可用 |
| S3 | 为 `billing` / `auth` 域补边界用例（并发结算、令牌过期、跨用户越权） | 高 | 每域 3–5 个用例 | 现有 56 / 42 个用例主要覆盖正常路径 |
| S4 | 清理仓库根重复的 `test_outbox.py` | 中 | 极低 | 属重复文件，非有效测试 |
| S5 | 统一 `DB_PATH` 的获取方式（例如改为运行时读 `task_engine.DB_PATH`），从根上消除 R10 | 中 | 涉及 3 个业务模块 | **属于业务代码改动**，本轮未做，需单独排期与回归 |
| S6 | 把 `run_tests.py audit` + 受保护 `unit/integration/api/backend` 接入 CI | 中 | 一次流水线配置 | 报告产出可直接作为 CI 工件 |
| S7 | 发布前固定跑「三层 + audit + 受保护 backend」回归脚本 | 中 | 一条脚本 | 当前这些命令全绿 |
| S8 | 逐步把 33 条 `registered-inferred` 复核为 `registered` | 低 | 每批若干文件 | 复核方式：按矩阵逐条对照源码 |

---

## 7. 当前测试工程成熟度评价

| 等级 | 定义 | 判定 |
|---|---|---|
| Level 1 | manual tests —— 手工跑、无分类 | 已超越 |
| Level 2 | structured tests —— 有分层与统一入口 | 已超越 |
| Level 3 | auditable test system —— 可枚举、可分类、可审计、有守卫与报告 | 已达成 |
| Level 4 | enforced test system —— 审计成为强制门禁、CI 阻断、**缺口闭环** | **当前** |

**当前：Level 4（enforced test system，强制化测试体系）**

达到 Level 4 的证据：

1. **可枚举**：39 个测试文件 100% 纳入 `discover_files()`，无遗漏。
2. **可分类**：**39 条 Schema 覆盖全部 39 个文件**，`legacy = 0`；分层 unit 5 / integration 24 / api 10，用例数可对账（32 + 154 + 67 = 253）。
3. **可审计**：静态隔离审计对数据库与网络两类出口给出 `文件:行号` 级证据，违例 0。
4. **有守卫且已闭环**：受保护全量后端 `253 cases / OK`；唯一缺口修复后经**独立反证**（把真实库替换成非数据库文件仍通过）确认无真实库访问。
5. **全绿门禁**：audit / unit / integration / api / backend（受保护）与未加守卫的 backend 全部 PASS。
6. **有报告**：即本文档，含总览、矩阵、已完成、未整理、风险、建议六节。

距离更高成熟度的剩余项（属于流程与覆盖，不属于体系缺失）：

1. CI 未接入（S6），审计尚未在流水线上自动阻断。
2. 33 条元数据为静态推断，未经逐条运行期验证（R3/S8）。
3. `DB_PATH` 绑定方式仍是系统性陷阱（R10/S5，需业务代码排期）。

---

## 附：验收命令与实测结果（2026-10-12 最终）

以下命令均在项目根目录、使用 `.venv\Scripts\python.exe` 执行。

| 命令 | 结果 | Files | Cases | Duration |
|---|---|---|---|---|
| `python scripts/run_tests.py audit` | **PASS** | 39 | —（不运行用例） | 0.06s |
| `python scripts/run_guarded_tests.py unit` | **PASS** | 5 | 32 | 0.34s |
| `python scripts/run_guarded_tests.py integration` | **PASS** | 24 | 154 | 14.48s |
| `python scripts/run_guarded_tests.py api` | **PASS** | 10 | 67 | 29.39s |
| **`python scripts/run_guarded_tests.py backend`（全量受保护）** | **PASS** | 39 | **253** | 43.66s |
| `python scripts/run_tests.py backend`（未加守卫，对照） | PASS | 39 | 253 | 43.72s |
| `python scripts/run_guarded_tests.py legacy` | n/a | 0 | 0 | — |

> `legacy` 现在是**空桶**，守卫运行器会按设计打印 `ERROR: legacy has no tests.` 并返回 2。
> 这是「已无未分类测试」的正常表现，不是失败。

最终验收输出：

```
Personal Agent Test Schema Audit
================================================
Layer: audit
Files: 39
Registered schemas: 39

unit           5 files
integration    24 files
api            10 files
legacy         0 files
...
Isolation audit (static source review)
Files reviewed: 39
Violations: 0
Warnings: 5
Duration: 0.06s
Result: PASS

PASS: Test Schema audit
```

```
Personal Agent Test Execution
================================================
Layer: backend
Files: 39
Cases: 253

Test case distribution:
  unit           32
  integration    154
  api            67
  legacy         0
================================================
...
Ran 253 tests in 43.660s

OK
PASS: backend, 253 tests
Duration: 43.66s
Result: PASS

PROTECTED TEST PASS: backend
```

隔离性独立验证（真实库防写入反证）：

```
loaded cases: 2
test_expired_lease_can_be_reclaimed ... ok
test_paused_and_archived_tasks_not_claimed ... ok
----------------------------------------------------------------------
Ran 2 tests in 0.154s

OK
restored tasks.db: identical=True
restored auth_users.db: identical=True
restored chat_requests.db: identical=True
restored reminders.db: identical=True
```

受保护全量运行（253 用例）前后真实数据库完整性：

```
tasks.db            unchanged=True
auth_users.db       unchanged=True
chat_requests.db    unchanged=True
reminders.db        unchanged=True
```
