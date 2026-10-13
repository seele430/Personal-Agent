# HANDOFF — Personal Agent 测试工程化（交给下一位 agent）

> 生成时间：2026-10-13
> 上一位 agent 的会话范围：测试工程化整理（登记 / 审计 / 分层契约 / 报告）+ 一个隔离缺口修复
> 本文档只描述**已核实的事实**。凡未经实测的推断，均已显式标注。

---

## 0. 一分钟摘要

- **目标**：把 Personal Agent 的测试从"个人维护的测试堆"提升为**可审计的工程测试体系**，且**不重构、不搬迁、不删除测试**。
- **状态：已完成并已推送。** 两个提交在远端 `main`：`e0e67e2`、`2d6566c`（HEAD）。
- **终态数据**：39 个测试文件 / 253 个用例，`unit 5(32) · integration 24(154) · api 10(67) · legacy 0`。
- **全部门禁绿**：`audit` PASS、受保护 `unit/integration/api/backend` 全 PASS。
- **未完成 / 待办**：① 用户提到的「登录会话问题」完全未查（见 §8）；② 可选真搬迁目录（见 §7 B）；③ CI 未接入。
- **头号风险**：`src/core` 里 `DB_PATH` 的 `from ... import` 绑定陷阱（见 §5 R10），以后写测试若踩到会**静默失效**。

---

## 1. 精确状态（接手前请自行复核一遍）

| 项目 | 值 |
|---|---|
| 仓库路径 | `C:\Users\123\Personal Agent` |
| 远端 | `origin` = `https://github.com/seele430/Personal-Agent.git`（HTTPS，**直连可用**） |
| 分支 / HEAD | `main` = `2d6566c89b557624b34b891ccd94e6ffb3a0099c` |
| 工作区 | **干净**（`git status --porcelain -uall` = 0 项） |
| 上游同步 | `## main...origin/main`（无 ahead/behind） |
| Python | **必须用** `C:\Users\123\Personal Agent\.venv\Scripts\python.exe`（3.12.10） |
| 本机 `py` / `python` 启动器 | **坏的**，不要用（`py -3.12` 报 `Unable to create process`；PATH 上的 `python` 是商店占位程序） |

最近提交：

```
2d6566c Make layer membership an explicit, queryable contract   ← 测试工程化收尾（分层契约）
e0e67e2 Add test engineering system and backend features        ← 测试工程化主体 + 后端/前端功能
a795911 Add chat request ownership storage and tests            ← 上一位开发者
```

> ⚠️ `e0e67e2` 把**之前所有未提交的工作一次性收进去了**（用户选择"全部一次提交"）：53 文件，含 `frontend/*`、`src/*`、`src/core/*.py`、`tests/*`。所以 `git log` 里那个提交**不等于**只做了测试工程化。

---

## 2. 怎么跑（可直接复制）

```powershell
cd "C:\Users\123\Personal Agent"
.\.venv\Scripts\Activate.ps1

# 分层索引（不跑用例）
python scripts/run_tests.py list
python scripts/run_tests.py list integration
python scripts/run_tests.py list --doc          # 重新生成 tests/TEST_LAYERS.md

# 审计（Schema + 层归属 + 文档同步 + 静态隔离）
python scripts/run_tests.py audit

# 受保护运行（推荐；带 SQLite/socket 守卫 + 无效 API key）
python scripts/run_guarded_tests.py unit
python scripts/run_guarded_tests.py integration
python scripts/run_guarded_tests.py api
python scripts/run_guarded_tests.py backend     # 全量 253

# 未加守卫的对照运行（仅用于对比，不要作为验收依据）
python scripts/run_tests.py integration
```

**实测结果（2026-10-13 复核）**：

| 命令 | 结果 | Files | Cases | Duration |
|---|---|---|---|---|
| `run_tests.py audit` | PASS（隔离违例 0 / 文档摘要一致） | 39 | — | 0.06s |
| `run_guarded_tests.py unit` | PASS | 5 | 32 | 0.34s |
| `run_guarded_tests.py integration` | PASS | 24 | 154 | 19.4s |
| `run_guarded_tests.py api` | PASS | 10 | 67 | 40.2s |
| `run_guarded_tests.py backend` | **PASS** | 39 | **253** | 46.5s |
| `run_guarded_tests.py legacy` | n/a（空桶，按设计 exit=2） | 0 | 0 | — |

---

## 3. 交付物清单（都在仓库里，已提交）

| 文件 | 作用 | 备注 |
|---|---|---|
| `tests/registry.py` | **核心**：39 条 Test Schema + 显式层归属契约 | 唯一事实来源；见 §4 |
| `scripts/run_tests.py` | 运行器：`unit/integration/api/legacy/backend/audit/list` | 分层选择查注册表；输出统一带 Duration/Result |
| `scripts/run_guarded_tests.py` | 受保护运行器（**未改动**） | 复用 run_tests + 守卫 |
| `scripts/test_safety.py` | 运行时守卫本体（**未改动**） | patch `sqlite3.connect` 与 socket；**非 OS 沙箱** |
| `scripts/audit_test_isolation.py` | **新增**：静态隔离审计 | 只读、标准库、不 import 测试模块 |
| `tests/TEST_LAYERS.md` | **新增**：分层索引（生成物） | 由 `list --doc` 生成，含 `membership-digest` 防手工漂移 |
| `TEST_ENGINEERING_REPORT.md` | **新增**：正式工程化报告 | 六节结构 + 验收输出；给用户/评审看 |
| `HANDOFF_TEST_ENGINEERING.md` | 本文件 | 给下一位 agent 看 |

---

## 4. 关键机制（接手必读）

### 4.1 层归属是元数据，不是目录

- `tests/` 根目录下**仍有 35 个测试文件平铺**，子目录只有 `unit/`(1)、`integration/`(3)。**这是有意为之**，不是漏做。
- 层归属来自 `tests/registry.py` 的 `TestSchema.layer`；`LAYER_MEMBERSHIP` 把它展开成「层 → 文件集合」。
- `run_tests.py / classify_file()` 的顺序：**注册表命中 → 用注册表的 layer**；否则回到目录约定（`unit/`、`integration/`、`api/`）；否则 `legacy`。
- 因此：**移动文件不会改变层归属**；新文件想进某一层，登记 Schema 即可。

### 4.2 两道"有牙齿"的校验（都在 `audit` 里）

| 校验 | 触发条件 | 后果 |
|---|---|---|
| 文档摘要 | `tests/TEST_LAYERS.md` 的 `membership-digest` 与代码不符 | `VIOLATION [document]` → audit **exit=2** |
| 磁盘对照 | 已登记但磁盘上文件不存在 | `VIOLATION [registry]` → 直接打断运行 |
| 缺登记告警 | 磁盘上存在却未登记 | `warning [membership]`（提示会落入 legacy 或靠目录兜底） |

> 已实测反证：把摘要改成 `deadbeef…` → audit FAIL；在副本里藏掉 `test_rss_errors.py` → audit exit=2。

### 4.3 隔离审计的边界（**别高估它**）

`audit_test_isolation.py` 是**静态源码审查**，判据：

- 数据库：出现 `chat_requests.db` / `tasks.db` / `auth_users.db` / `reminders.db` → 违例；若路径解析在临时根下（`TemporaryDirectory`/`mkdtemp`/`self.temp`/被 patch 的 `*_DB_PATH`）→ 降级为提示。
- 网络：`requests|httpx` 的 HTTP 方法、`urlopen`、裸 socket `connect/connect_ex/sendto/sendall` → 违例；同行有 `patch/mock/assertRaises/guard` 或位于守护块内 → 跳过；`TestClient`（进程内 ASGI）不算真实网络。
- Schema 一致性：已登记 Schema 出现 `database="real"` 或 `external_network="real"` → 违例。

**它不证明运行期一定不触网**；真正的运行期拦截靠 `scripts/test_safety.py`（且仅覆盖 SQLite + Python socket）。

---

## 5. 风险与坑（按严重度，全部有实测证据）

| # | 风险 | 等级 | 证据 / 位置 | 说明 |
|---|---|---|---|---|
| **R10** | **`DB_PATH` 绑定陷阱**：`monitor_registry.py:8`、`monitor_dispatcher.py:13`、`task_run_history.py:9` 都写 `from src.core.task_engine import DB_PATH`，**各模块各绑一份引用** | **高（最容易踩）** | 实测：替换 `task_engine.DB_PATH` 后 `task_run_history.DB_PATH is task_engine.DB_PATH == False` | 测试里 patch 源头或只 patch 部分模块 → **静默失效**，运行时才炸。写新测试时**必须 patch 实际使用该常量的那个模块** |
| R3 | 33 条 Schema 是**静态阅读推断**的，非运行期验证 | 中 | `tests/registry.py`，标 `registered-inferred` | 元数据可能有个别字段判断偏差（尤其 `authentication`） |
| R4 | 计费/预算域 6 文件 56 用例，涉及金额与并发 | 高 | `test_ai_budget*.py` / `test_ai_pricing.py` / `test_ai_usage.py` / `test_chat_budget_http.py` / `test_chat_reconciliation_http.py` | 回归失败代价高 |
| R5 | 认证域 5 文件 42 用例 | 高 | `test_auth_store.py` / `test_auth_api.py` / `test_bootstrap_owner.py` / `test_login_rate_limit.py` / `test_chat_secure_api.py` | 回归 = 越权风险 |
| R6 | `tests/test_rss_errors.py` 依赖真实 `requests` 库，靠 patch 上层函数断网 | 中 | 全部用例 patch `rss_monitor.fetch_rss` | 若 patch 目标改名 → 退化为真实外呼 |
| R7 | 守卫非 OS 沙箱，只管 SQLite + socket | 中 | `scripts/test_safety.py:1-14` | 子进程、非 socket IO 不受约束 |
| R8 | 仓库根 `test_outbox.py` 与 `tests/test_outbox.py` **字节级相同** | 低 | SHA256 均 `468186DDB06F3DA22238E7E612102CF347E497A5702A6D45285173766E27DF86` | 不在运行器扫描范围（只扫 `tests/**`）；改动易只改一份 |
| R11 | `tests/test_test_safety.py` 与 `tests/integration/test_guarded_asyncio.py` 用 `Path(__file__).resolve().parents[2]` 推项目根 | 中 | 两个文件各 1 处 | **一旦移动这两个文件，`parents[N]` 必须同步改**；详见 §7 B |

### 5.1 本机环境坑（会直接影响 git / 运行）

- **git 远端是 HTTPS，已直连可用**。此前仓库残留的 `http.https://github.com.proxy = http://127.0.0.1:7890` **已被上一位 agent 移除**（7890 无进程监听，是死配置）。**如果又出现 `git fetch` 失败**，先查 `git config --local --get-regexp proxy`，不要再配回 7890（那是 Taishan 的口，会烧用户余额）。
- **SSH/443 通路可用且零成本**：`~/.ssh/config` 把 `github.com` 映射到 `ssh.github.com:443`。推送可用一次性 URL：`git push git@github.com:seele430/Personal-Agent.git main:main`。
- **`git status` 的 `[ahead]` 只有在 fetch 成功后才可信**：曾出现 fetch 撞死代理 → 本地 `origin/main` 滞留旧值 → 误报 `[ahead 1]`（推送其实已成功）。
- **PowerShell 里 git 的进度输出走 stderr**，会被渲染成红色 `NativeCommandError`——**看 `$LASTEXITCODE`，不要看颜色**。
- **中文控制台输出会乱码**（cp936）。要看中文，用 `[Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($path))` 或直接把输出重定向到文件再用 UTF-8 读。

---

## 6. 已解决的缺口（背景，避免重复调查）

`tests/test_monitor_lease.py` 曾经在受保护运行下抛
`TestSafetyViolation: Test cannot access project database: ...\tasks.db`。

- **根因**：`setUp` 只 patch 了 `monitor_registry.DB_PATH` / `monitor_dispatcher.DB_PATH`；而 `claim_due_tasks() → init_dispatcher_db() → init_run_history()` 用的是 `task_run_history.DB_PATH`（未 patch）。深层原因是 R10 的 `from ... import` 绑定。
- **修复**：该测试 +12 行 / 0 删除，补齐 `task_engine`、`task_run_history` 的 `DB_PATH` patch（**断言未改、业务代码未改**）。
- **独立反证**：把 4 个真实库文件临时替换成 `NOT-A-DATABASE-POISON` 再跑该测试 → `Ran 2 tests ... OK`；跑完还原，SHA256 与还原前一致。
- **现状**：受保护全量 253 用例跑完，4 个真实库 SHA256 与 mtime **均未变**。

---

## 7. 候选待办（按优先级）

### A. 高价值、低风险

| # | 事项 | 说明 |
|---|---|---|
| A1 | 复核 33 条 `registered-inferred` → 改成 `registered` | 逐条对照源码核对 `domain/database/authentication/external_network/risk`；改 `registry.ORIGINAL_SCHEMAS` 或加显式字段 |
| A2 | 把 `audit` + 受保护四层接进 CI | 当前无 CI。建议 GitHub Actions；注意 GitHub 上 **Python 3.11/3.12 下必须能离线跑**（测试全离线，应无问题） |
| A3 | 给 `external_network="mocked"` 的文件补"patch 目标存在"自检 | 防 R6 类静默退化 |

### B. 真搬迁目录（**需先与用户确认**，因为要改测试）

用户问过"测试没分到各自文件夹算工程化吗"。已做**副本干跑**评估（复制整个 `tests/` + `scripts/` 到临时目录，35 文件按层真实搬入后跑全量）：

| 观测 | 搬迁前 | 搬迁后（干跑） |
|---|---|---|
| 物理布局 | 根 35 / unit 1 / integration 3 / api 0 | 根 0 / unit 5 / integration 24 / api 10 |
| 用例 | 253 | 253（无丢失） |
| audit | PASS | PASS |
| 受保护全量 | 253 OK | **253 中 1 failure** ❌ |

**唯一失败点**：`integration/test_test_safety.py::test_non_temporary_path_blocked`
（它用 `ROOT = Path(__file__).resolve().parents[2]` 推项目根，再用 `ROOT.parent / "safety_example.db"` 构造"项目外且非临时"路径来断言守卫拦截；文件下移一层后 `ROOT.parent` 落进系统临时目录，守卫不再拦截 → `AssertionError: TestSafetyViolation not raised`）。

搬迁成本：35 次移动 + registry 35 个键名同步 + 新建 `tests/api/__init__.py` + 改 2 处 `parents[N]` + 更新文档/audit 路径 + git 35 条 rename。
**收益可用现有 `list`/`TEST_LAYERS.md` 契约以零测试改动拿到**，故当时选择不搬。

### C. 未验证 / 未做

| # | 事项 | 现状 |
|---|---|---|
| C1 | **登录会话问题** | **完全未调查**。线索：用户上一条消息提到"继续修复现在的登录会话问题"；仓库里有 `frontend/src/AuthPreview.tsx`、`src/auth_api.py`、`tests/test_login_rate_limit.py`、`tests/test_auth_api.py`。**接手动手前必须先问用户具体现象/复现路径** |
| C2 | 前端测试未纳入本 Runner | `frontend/` 用 Playwright，由 `scripts/test.ps1 -Suite frontend` 单独跑（**未实测**） |
| C3 | `DB_PATH` 写法统一（根除 R10） | 需改 `src/core` 3 个模块（业务代码），未做 |

---

## 8. 给下一位 agent 的建议动作顺序

1. **先复核** §1 的 git 状态与 §2 的五条命令（约 1 分钟）。
2. **问用户**「登录会话问题」的具体现象（§7 C1）——这是唯一有明确用户诉求的未完成项。
3. 若要动测试：**先读 §4.1 与 §5 R10**，否则极可能写出"patch 了但没生效"的测试。
4. 改完 `registry.py` 后**必须**跑 `python scripts/run_tests.py list --doc`（否则 audit 会因文档摘要过期而 FAIL）。
5. 提交前：`run_tests.py audit` + 受保护四层全绿；推送后按"`status -sb` 无 ahead + `ls-remote` SHA 一致"两步核验。

---

## 9. 不属于本次范围的说明

- 本次**未修改任何业务代码**（`src/**` 只在 `e0e67e2` 里作为"先前未提交工作"被一起提交，不是本次会话改的）。
- 本次**未删除、未移动任何测试文件**；唯一被改的测试是 `tests/test_monitor_lease.py`（§6，+12 行隔离修复）。
- `TEST_ENGINEERING_REPORT.md` 是**给用户/评审**看的正式文档；本文件是**给接手 agent** 看的操作手册。两者数据一致，但用途不同。
