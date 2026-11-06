# HANDOFF（2026-11-05 更新）

> 交接给下一个人 / 下一个会话。读完这份就能接着干，不用翻聊天记录。

## 0. 一句话现状

平台主线（执行/取消/审批/轨迹/生命周期/配额/对账/认证/限流/审计/队列/观测/压测）完成；
**本轮新增**意图路由（规则层 + 评估集 + 审批闸门）与弹性控制面（扩缩容控制器 + 对照实验 + 残留校验）。
全量 **95 文件 / 812 用例 PASS**；CI 三 job 全绿。剩余：前端控制台页、正式长压测、G3 视频。

## 1. 仓库与入口

| 项 | 值 |
|---|---|
| 路径 | `C:\Users\123\Personal Agent` |
| 分支 | `main`（与 `origin/main` 同步） |
| 远端 | `git@github.com:seele430/Personal-Agent.git` |
| 运行时 | `.venv\Scripts\python.exe`（Python 3.12） |

### 启动（开发模式）

```powershell
cd "C:\Users\123\Personal Agent"
.\scripts\run-dev.ps1 -DataDir "$env:TEMP\pa-demo" -NoNtfy
.\scripts\run-dev.ps1 -Stop
```

- 前端 <http://127.0.0.1:5173>；API `127.0.0.1:8000`
- 首次启动：登录页自动变「创建站长账号」；忘记密码走「忘记密码？」（仅本机）

## 2. 本轮新增（2026-11-05）

| 模块 | 文件 | 说明 |
|---|---|---|
| 意图路由（规则层） | `src/routing/router.py` | 5 intent + clarify；只给约束，不选节点 |
| 路由事件 | `src/routing/events.py` | `route_decided` 写入 trajectory |
| 审批闸门 | `src/routing/policy.py` | 高危必须审批，**不看置信度** |
| 安全降级 | `src/routing/fallback.py` | 换 provider，不降风险/权限 |
| 评估集 | `tests/data/router-eval.jsonl` | 160 条，90/35/35，test 冻结哈希 |
| 评估脚本 | `scripts/eval_router.py` | accuracy 94.3%、macro-F1 0.963、高危漏审批 0 |
| 垂直切片 | `scripts/demo_route_pipeline.py` | 一句话 → 沙箱 → `chart.png` → 完整 trace |
| 扩缩容 | `src/platform/autoscale.py` | `desired_capacity` + 稳定扩容 + 冷却缩容 + 事件 |
| 弹性对照 | `scripts/autoscale_compare.py` | 峰值积压 -71.9%，平均积压 -98.2% |
| 残留校验 | `scripts/verify_w34_residue.py` | 目录/DB/进程残留 = 0 |
| 前端控制台 | `frontend/src/PlatformView.tsx`、`src/console_api.py` | 侧栏「平台」一屏：路由/队列/节点/执行 |
| 一屏控制台（CLI） | `scripts/console_overview.py` | 路由/节点/沙箱/执行一屏 |
| 演示总入口 | `scripts/demo_w4.py` | 路由 + 弹性 + 回收，一条命令 |

报告：`docs/ROUTER-REPORT.md`、`docs/ELASTIC-REPORT-RESULTS.md`、`docs/PLAN-ROUTER-ELASTIC-20261105.md`。

## 3. 验收与证据

| 项 | 结果 |
|---|---|
| 全量后端回归 | **95 文件 / 812 用例 PASS** |
| 前端构建 + Playwright | 34/34 PASS（CI） |
| CI | run 41996586242 三 job success（后续提交沿用同一 workflow） |
| 路由评估 | test 94.3% / macro-F1 0.963 / 高危漏审批 0 |
| 弹性对照 | 峰值积压 64→18，平均积压 26.76→0.49，丢弃 296→0 |
| 回收残留 | 0 / 0 / 0 |
| 证据目录 | `docs/evidence/`、`docs/bench/` |

## 4. 怎么跑

```powershell
# 全量回归（约 8—13 分钟）
.\.venv\Scripts\python.exe scripts\run_guarded_tests.py backend

# 路由评估
.\.venv\Scripts\python.exe scripts\eval_router.py --split test

# 垂直切片 / 一屏控制台
.\.venv\Scripts\python.exe scripts\demo_route_pipeline.py
.\.venv\Scripts\python.exe scripts\console_overview.py --data-dir "$env:TEMP\pa-console-demo"

# 弹性对照 / 残留校验 / 总演示
.\.venv\Scripts\python.exe scripts\autoscale_compare.py
.\.venv\Scripts\python.exe scripts\verify_w34_residue.py
.\.venv\Scripts\python.exe scripts\demo_w4.py
```

## 5. 已知缺口（按优先级）

1. ~~W4.1 前端控制台~~ 已完成：侧栏「平台」视图 + `GET /console/overview`（CLI `console_overview.py` 保留）。
2. ~~正式长压测~~ 已完成：2/4/8/12 RPS 各 10 分钟，8 RPS 重复 3 次，含 CPU/RSS 采样；拐点 4 RPS，12 RPS 触发 6.4% `QUEUE_FULL`（`docs/bench/ELASTIC.md`）。
3. **路由分类器层暂缓**：规则层已达标；触发条件是准确率跌破 90% 或新增 intent。
4. ~~路由接入对话流~~ 已完成：`agent_stream.run_chat_stream` 入口先路由，歧义请求直接澄清。
5. **G3 60 秒视频**：命令已冻结（`docs/G3.md`），待录。
6. ~~邀请码 UI~~ 已决定不做：项目改为**仅个人使用**，无注册/邀请入口（`/auth/register`、`/auth/invite` 未注册）。
7. **D31–D36 砍单**：本机无 Docker/C++ 工具链。

## 6. 环境与坑（新增）

- **DSH 环境里 `HTTP_PROXY=127.0.0.1:7890` 指向死代理**：Python/curl 会卡住；清空 `HTTP_PROXY/HTTPS_PROXY` 或对 `curl` 加 `--ssl-no-revoke` 才能直连。
- **PowerShell 5.1 读无 BOM UTF-8 `.ps1` 按 GBK 解析** → 脚本写纯 ASCII 或带 BOM。
- **浏览器自动填充不触发 onChange** → 表单提交时直接从 `form.elements` 读值。
- **Windows 临时目录被 worker 占用** → 测试用 `TemporaryDirectory(ignore_cleanup_errors=True)` 或 `wait_for_exec_worker()`。
- **`.env.local` 是 gitignored**：Vite 代理目标用 `VITE_API_TARGET` 控制。

## 7. 关键文档

| 文档 | 作用 |
|---|---|
| `docs/PLAN-ROUTER-ELASTIC-20261105.md` | 计划与进度总表 |
| `docs/design-intent-router.md` | 路由职责边界与决策对象 |
| `docs/ROUTER-REPORT.md` | 路由评估结果与局限 |
| `docs/ELASTIC-REPORT-RESULTS.md` | 弹性结果与局限 |
| `docs/platform-contract.md` / `docs/trajectory-schema.md` | 平台契约与轨迹事件 |
| `docs/journal.md` | 逐日记录 |
