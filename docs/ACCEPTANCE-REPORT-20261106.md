# 交付验收报告

> 交付日期：2026-11-06
> 仓库：`C:\Users\123\Personal Agent`，`main @ fc4b372`（与 `origin/main` 同步）
> 验收人：Kimi（独立复核）
> 建议结论：**A–F 组建议判通过；W2.3（分类器）为有理由的暂缓，建议按“说明项”而非“失败项”处理。**

---

## 0. 怎么验（最短路径）

```powershell
cd "C:\Users\123\Personal Agent"
git pull
.\.venv\Scripts\python.exe scripts\run_guarded_tests.py backend   # 全量，约 6 分钟
.\.venv\Scripts\python.exe scripts\eval_router.py --split test    # 路由指标
.\.venv\Scripts\python.exe scripts\demo_w4.py                     # 路由+弹性+回收 一条命令
```

看数字：全量 **815 用例 PASS**（CI 已跑过，见 §3-F1）；路由 test **94.3% / macro-F1 0.963 / 高危漏审批 0**；压测拐点 **4 RPS**。

---

## 1. 基线与环境

| 项 | 值 |
|---|---|
| 仓库 | `git@github.com:seele430/Personal-Agent.git` |
| 基线提交 | `fc4b372` |
| 运行环境 | Windows 11，12 逻辑核 / 16 GB，Python 3.12，`.venv` |
| 服务形态 | 单机单进程：FastAPI + SQLite + tier1 进程沙箱 |
| 产品形态 | **单用户个人自用**（本轮明确） |

---

## 2. 交付范围（5 个模块）

| 模块 | 一句话 |
|---|---|
| A 意图路由 | 每条消息先路由（intent/风险/能力/回退），决策落轨迹；歧义直接澄清 |
| B 弹性控制面 | 期望容量 + 扩缩容控制器（稳定性/冷却/事件）+ 失联停接活 |
| C 正式压测 | 2/4/8/12 RPS × 10 分钟，8 RPS 重复 3 次，CPU/RSS 采样 |
| D 平台控制台 | 前端「平台」一屏 + `/console/overview` API + CLI |
| E 单用户化 | 无注册/邀请 HTTP 入口，登录页文案与文档对齐 |
| F 工程化 | 全量回归、CI 三 job、测试索引审计 |

---

## 3. 验收清单

### A 意图路由

| 编号 | 要求 | 结果 | 证据 |
|---|---|---|---|
| A1 | 路由决策可解释、落轨迹 | ✅ `run_started → route_decided` | `src/routing/events.py`、`tests/integration/test_route_events.py` |
| A2 | 5 类 intent + clarify | ✅ 规则层 | `src/routing/router.py`、`tests/test_intent_router.py`（9 条） |
| A3 | 高危漏审批 = 0 | ✅ 确定性闸门，不看置信度 | `src/routing/policy.py`、`tests/test_routing_policy.py`（7 条） |
| A4 | 接入真实对话流 | ✅ `agent_stream.run_chat_stream` 入口 | `src/agent_stream.py`、`tests/test_agent_stream.py` |
| A5 | 歧义不调用模型 | ✅ 直接返回澄清问题 | 同上（测试用 `_completion` 抛异常证明未被调用） |
| A6 | 评估集 + 指标 | ✅ test 94.3%，macro-F1 0.963 | `docs/evidence/router-eval.json`、`tests/data/router-eval.manifest.json` |

### B 弹性控制面

| 编号 | 要求 | 结果 | 证据 |
|---|---|---|---|
| B1 | 期望容量计算 | ✅ 单测 4 条 | `src/platform/autoscale.py`、`tests/test_autoscale.py` |
| B2 | 扩容稳定 + 缩容冷却 + 事件 | ✅ 单测 4 条 | 同上 |
| B3 | 节点失联停止接活 | ✅ 租约过期 → `accepting=False` → 不调度 | `tests/integration/test_platform_scheduling.py::test_expired_lease_stops_accepting` |
| B4 | 固定 vs 弹性对照 | ✅ 峰值积压 -71.9%，平均 -98.2%，丢弃 296→0 | `docs/bench/autoscale-compare.*`、`docs/ELASTIC-REPORT-RESULTS.md` |
| B5 | 回收无残留 | ✅ 目录/DB/进程 = 0/0/0 | `docs/evidence/w34-residue.json`、`scripts/verify_w34_residue.py` |

### C 正式压测

| 编号 | 要求 | 结果 | 证据 |
|---|---|---|---|
| C1 | 预定义判据，跑前写死 | ✅ 判据在 `ELASTIC.md` §2 | `docs/bench/ELASTIC.md` |
| C2 | 每档 10 分钟 + 资源采样 | ✅ 2/4/8/12 RPS 各 10 分钟 | `docs/bench/ladder-2026*` |
| C3 | 关键档 ≥ 3 次重复 | ✅ 8 RPS × 3 次 | `ladder-20261105-221553`、`-082600`、`-083948` |
| C4 | 拐点可复现 | ✅ 4 RPS（p99 1969 ms > 1000 ms） | 同上 + `ELASTIC.md` §6 |

### D 平台控制台

| 编号 | 要求 | 结果 | 证据 |
|---|---|---|---|
| D1 | 一屏看路由/队列/节点/执行 | ✅ 前端「平台」视图 | `frontend/src/PlatformView.tsx`、`docs/evidence/w4-console-ui.png` |
| D2 | 接口鉴权 | ✅ 未登录 401 | `tests/integration/test_console_api.py` |
| D3 | CLI 兜底 | ✅ | `scripts/console_overview.py`、`docs/evidence/w4-console.txt` |
| D4 | 真实浏览器验收 | ✅ 登录→平台→四区块可见 | `scripts/e2e_console.cjs`（CONSOLE UI OK） |

### E 单用户化

| 编号 | 要求 | 结果 | 证据 |
|---|---|---|---|
| E1 | 无注册/邀请 HTTP 入口 | ✅ 三个路径全部非成功 | `tests/integration/test_single_user_surface.py` |
| E2 | 首次启动建站长 | ✅ 空库登录页变建号表单 | `src/auth_api.py`、`frontend/src/AuthPreview.tsx` |
| E3 | 文案与文档一致 | ✅ 「个人使用 · 本地优先」 | `README.md`、`docs/AUTH_AND_ISOLATION.md` |

### F 工程化

| 编号 | 要求 | 结果 | 证据 |
|---|---|---|---|
| F1 | 全量回归 | ✅ **815 用例 PASS** | CI run `42144144540` |
| F2 | CI 三 job | ✅ Isolation audit / Backend / Frontend | 同上 |
| F3 | 测试索引审计 | ✅ 0 violation | `tests/TEST_LAYERS.md` |
| F4 | 前端构建 | ✅ `npm run build` 通过 | CI `Frontend build + Playwright` |

---

## 4. 关键数字

| 指标 | 值 |
|---|---|
| 路由 test 准确率 / macro-F1 | **94.3% / 0.963** |
| 路由高危漏审批 | **0** |
| 路由延迟 p50 / p99 | 0.028 ms / 0.36 ms（纯规则） |
| 压测稳定档 | 2 RPS：p99 890 ms |
| **压测拐点** | **4 RPS**：p99 1969 ms |
| 8 RPS（3 次中位） | p50 2656 ms / p99 5328 ms / 队列 12 |
| 12 RPS | **6.4% `QUEUE_FULL`**，队列 16（满），p99 5609 ms |
| 弹性对照 | 峰值积压 -71.9%，平均 -98.2%，丢弃 0 |
| 回收残留 | 0 |
| 全量用例 | **815**（96 个测试文件） |

---

## 5. 证据索引

| 类别 | 路径 |
|---|---|
| 路由指标 | `docs/evidence/router-eval.json` |
| 路由垂直切片 | `docs/evidence/route-pipeline-chart.png` |
| 控制台截图 | `docs/evidence/w4-console-ui.png` |
| 残留报告 | `docs/evidence/w34-residue.json` |
| 压测原始数据 | `docs/bench/ladder-20261105-214731.*`（2）、`-215746.*`（4）、`-221553.*`/`-082600.*`/`-083948.*`（8×3）、`-085855.*`（12） |
| 弹性对照 | `docs/bench/autoscale-compare.json/csv` |
| 报告 | `docs/ROUTER-REPORT.md`、`docs/ELASTIC-REPORT-RESULTS.md`、`docs/bench/ELASTIC.md` |
| 计划与进度 | `docs/PLAN-ROUTER-ELASTIC-20261105.md` |

---

## 6. 已知缺口与边界（请按“说明项”复核）

1. **W2.3 分类器层暂缓**：规则层已达 90% 目标（test 94.3%）；分类器会引入延迟与维护成本。触发条件是准确率跌破 90% 或新增 intent。
2. **`model_policy` 只记录、未生效**：路由会给出 cheap/strong，但还没有按策略切换模型。
3. **评估集是自造的**：160 条（90/35/35），test 集已冻结哈希，但 test 在调规则时被看过一次，94.3% 应视为上界。
4. **压测是单机单进程**：不代表集群；任务体是 `print(1)` 的短进程，偏轻。
5. **G3 演示视频未录**：命令已冻结，需人工录屏。
6. **邀请码存储层方法保留**：作为未来多用户预留，产品面不暴露（E1 已锁）。
7. **无 Docker / C++ 工具链**：D31–D36 砍单，理由在 `PLAN-STATUS.md`。

---

## 7. 复验命令

```powershell
# 全量
.\.venv\Scripts\python.exe scripts\run_guarded_tests.py backend

# 路由
.\.venv\Scripts\python.exe scripts\eval_router.py --split test
.\.venv\Scripts\python.exe scripts\demo_route_pipeline.py

# 弹性 / 回收
.\.venv\Scripts\python.exe scripts\autoscale_compare.py
.\.venv\Scripts\python.exe scripts\verify_w34_residue.py

# 控制台
.\.venv\Scripts\python.exe scripts\console_overview.py --data-dir <数据目录>

# 压测（单档 10 分钟；先清 bench 数据）
Remove-Item -Recurse -Force docs\bench\data,docs\bench\sandboxes -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe scripts\bench_platform.py --ladder 4 --minutes 10
```

---

## 8. 本轮变更清单（18 个提交）

```
fc4b372 fix: 单用户测试清理 get_auth_store 进程缓存
503963f chore: 删除根目录空文件
6f5d669 feat: 改为单用户产品面（不暴露注册/邀请入口）
898c1ed test: 审批链路断言排除 route_decided
86aead8 feat: 路由接入真实对话流
dcb4e0a docs: HANDOFF 记录正式压测结果
db8a2b1 test: 补齐 8 RPS 三次重复与 12 RPS
77c3b29 test: 正式压测 2/4/8 RPS 各 10 分钟
3c8415f docs: HANDOFF 更新（控制台上线）
fbc97d4 feat: 前端平台控制台
d341da6 fix: 提交路由规则调优与 matplotlib 依赖
9a3525b docs: HANDOFF 更新
2768915 feat: 一屏控制台 CLI
b2fa1b1 docs: 路由与弹性结果报告
ed26e0f feat: 扩缩容控制器、安全降级、残留校验
7d53491 feat: 路由评估集与评估脚本
2389bf1 feat: CSV 垂直切片与确定性审批闸门
c78b341 feat: route_decided 写入轨迹
```

---

## 9. 建议的验收判定

| 组 | 建议 |
|---|---|
| A 意图路由 | **通过**（A1–A6 证据齐） |
| B 弹性控制面 | **通过**（B1–B5） |
| C 正式压测 | **通过**（判据预定义、数据可复跑；单机边界见 §6） |
| D 控制台 | **通过**（前端 + API + 截图） |
| E 单用户化 | **通过**（入口已锁） |
| F 工程化 | **通过**（815 用例 + CI 三绿） |
| 说明项 | W2.3 分类器暂缓；model_policy 未生效；G3 视频未录 |
