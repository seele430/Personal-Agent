# HANDOFF（2026-11-04）

> 交接给下一个人 / 下一个会话。读完这份就能接着干，不用翻聊天记录。

## 0. 一句话现状

沙箱平台的主线（执行 / 取消 / 审批 / 轨迹 / 生命周期 / 配额 / 对账 / 认证 / 限流 / 审计 / 队列 / 观测 / 压测）已经完成并合并到 `main`；
**产品第一次能被真人用起来**（首次运行建站长、忘记密码、SPA 刷新不 404）。
唯一还差的门是 **G3 的 60 秒演示视频**（命令已冻结，视频等产品用顺手后再录）。

## 1. 仓库与入口

| 项 | 值 |
|---|---|
| 路径 | `C:\Users\123\Personal Agent` |
| 分支 | `main` @ `94ccc6a`（本地 = 远端 `origin/main`） |
| 远端 | `git@github.com:seele430/Personal-Agent.git` |
| 运行时 | `.venv\Scripts\python.exe`（Python 3.12） |

### 启动（开发模式）

```powershell
cd "C:\Users\123\Personal Agent"
.\scripts\run-dev.ps1 -DataDir "$env:TEMP\pa-demo" -NoNtfy   # 后端三件套 + 前端 5173
.\scripts\run-dev.ps1 -Stop                                   # 收尾
```

- 前端：<http://127.0.0.1:5173>；API：`127.0.0.1:8000`
- `-DataDir` 强烈建议用临时目录：不碰项目根目录的真实数据。
- 已有数据目录 `%TEMP%\pa-demo`，账号 `seele`（密码在交接人口头/私下传递，不要写进仓库）。

### 第一次用（空数据目录）

打开前端 → 登录页自动变成 **「创建站长账号」**（不再需要终端脚本）。
应急情况下仍可离线建号：

```powershell
.\.venv\Scripts\python.exe scripts\bootstrap_owner.py --username seele --password-env 环境变量名
```

### 忘记密码

登录页 →「忘记密码？」→ 输入新密码。**仅限本机（127.0.0.1）**，重置后旧会话全部失效。
命令行应急：`scripts\reset_password.py`。

## 2. 最近这一段做了什么（按提交倒序）

| 提交 | 内容 |
|---|---|
| `94ccc6a` | SPA 回退：`/login` 刷新不再 404；未知 `/api/*` 仍 404 |
| `d1f2aed` | 登录/建号/重置改为提交时读表单值（浏览器自动填充不触发 onChange） |
| `2b56fb7` | 网页版「忘记密码」（仅本机自助重置 + 旧会话吊销） |
| `5aefba6` | 首次运行在登录页创建站长账号（`/auth/state` + `/auth/bootstrap`） |
| `e9a086a` | 前端聊天用例对齐「发送中/停止生成」新状态 |
| `401b032` | 建站长失败时提示实际密码字节数 |
| `180c9be` | `bootstrap_owner` 支持环境变量密码，非 tty 不卡死 |
| `8d4b40c` | 离线建站长的脚本（已被网页表单取代，保留作应急） |

更早的 D11–D40 见 `docs/journal.md`（每天三行）与 `docs/PLAN-STATUS.md`（对照计划表）。

## 3. 验收与证据

| 项 | 结果 |
|---|---|
| 全量后端回归 | **86 文件 / 766 用例 PASS**（`scripts/run_guarded_tests.py backend`） |
| 前端构建 | `npm run build`（tsc + vite）通过 |
| 端到端（忘记密码） | `scripts/e2e_reset_password.cjs`：真实 Chromium 走完重置 → 新密码登录 → 进入工作空间 |
| 证据目录 | `docs/evidence/`（每轮一份 txt/json/png） |
| 计划完成度 | `docs/PLAN-STATUS.md`（含 D31–D36 砍单理由） |

## 4. 怎么跑测试 / 演示 / 压测

```powershell
# 全量（约 3—4 分钟）
.\.venv\Scripts\python.exe scripts\run_guarded_tests.py backend

# 演示（G3 冻结版，3 条命令，约 25 秒）
.\.venv\Scripts\python.exe scripts\demo_g3.py

# 压测（固定 RPS 阶梯）
.\.venv\Scripts\python.exe scripts\bench_platform.py --ladder 2,4,8,12 --duration 10

# 真实链路
.\.venv\Scripts\python.exe scripts\verify_platform_v1.py     # SDK 打真实端口
.\.venv\Scripts\python.exe scripts\verify_d22_reconcile.py   # 重启对账
.\.venv\Scripts\python.exe scripts\verify_g2_chain.py        # 一句话 3 工具 + 轨迹
```

> Windows 上跑测试前把 `TEMP`/`TMP` 指到工作区内的目录，避免临时目录权限问题。

## 5. 已知问题与待办（按优先级）

1. **G3 缺 60 秒视频**：命令已冻结（`docs/G3.md`），`demo_g3.py` 实测 3/3 PASS。
   录屏用 Game Bar（`Win+Alt+R`）最省事；ffmpeg 抓窗口这条路在 Agent 会话里抓不到（无交互桌面）。
2. **邀请码流程没有 UI**：除站长外的账号，目前只有后端接口，前端没有「邀请/注册」页。
3. **`/auth/bootstrap` 只靠「空库 + 登录限流」**，没限制来源（重置密码有回环限制）。
4. **D31–D36 被砍**：本机无 Docker、无 C++ 工具链（理由见 `PLAN-STATUS.md`）。
5. **限流/队列是进程内的**：多 worker 各算各的（SLO 与威胁模型都写明）。
6. **审计不记命令正文**：要查命令得拿 `exec_id` 去查 exec 记录。

## 6. 环境与坑（都是实际踩过的）

- **PowerShell 5.1 读无 BOM 的 UTF-8 `.ps1` 会按 GBK 解析** → 脚本请写纯 ASCII，或写成 UTF-8 with BOM。
- **反引号续行不可靠**：`ffmpeg` 的参数行曾被拆坏，长命令尽量写一行。
- **Windows 文件锁**：临时目录里有 sqlite 连接未关时，teardown 报 `WinError 32`；
  测试里用 `wait_for_exec_worker()` 等 worker 退场，或给 `TemporaryDirectory` 加 `ignore_cleanup_errors=True`。
- **浏览器自动填充不触发 onChange**：表单提交时直接读 `form.elements`，别只信 React state。
- **`uvicorn` 起在 8000 而前端独立在 5173**：改后端代码必须重启后端，前端热更新不代表后端生效。

## 7. 接手第一小时建议

1. `git pull` → 确认 `main` = `94ccc6a` 之后的最新提交。
2. 跑一次全量（确认 766 全绿），再跑 `demo_g3.py`（确认 3/3）。
3. `run-dev.ps1 -DataDir %TEMP%\pa-handoff` 起一套，亲手走一遍：
   列目录 → 写文件（审批）→ 取消 → 看轨迹 → 开 `/metrics`。
4. 有 Docker / C++ 的机器上，再补 D31–D36；否则维持现状并把 `PLAN-STATUS.md` 更新。
5. 录 G3 视频、标绿 `docs/G3.md` 与 `PLAN-STATUS.md`。

## 8. 关键文档索引

| 文档 | 作用 |
|---|---|
| `docs/platform-contract.md` | 平台接口/状态机/错误码（含每轮实现注记） |
| `docs/threat-model.md` | 信任边界与已知缺口（认证、隔离、审计） |
| `docs/trajectory-schema.md` | 轨迹事件 schema v1（冻结） |
| `docs/RUNBOOK.md` | 10 类故障的处置 |
| `docs/architecture.md` | 一张图 + 三个 trade-off |
| `docs/SLO.md` / `docs/bench/SATURATION.md` | SLO 草案与饱和曲线（拐点 8–12 RPS） |
| `docs/postmortem.md` | 默认节点租约 bug 的真实复盘 |
| `docs/PLAN-40D.md` / `docs/PLAN-STATUS.md` | 计划表与完成度对照 |
| `docs/journal.md` | 逐日三行记录 |
