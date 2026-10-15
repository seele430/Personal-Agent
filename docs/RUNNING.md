# 运行手册

把项目跑起来需要 **三个常驻后端进程**；开发时再加一个前端 dev server。

| 进程 | 命令 | 干什么 | 少了它会怎样 |
| --- | --- | --- | --- |
| **API** | `uvicorn src.main:app --port 8000` | 面板与全部接口 | 界面打不开，接口全 404 |
| **Scheduler** | `python -m src.scheduler` | 到期扫描：提醒每 5 秒、监控任务每 60 秒 | 任务建了**永远不会跑**（`next_run_at` 到期也没人理） |
| **Outbox** | `python -m src.core.outbox_service` | 通知投递，每 15 秒轮询 | 通知永远停在 `pending`，**站内收件箱永远是空的** |
| **Frontend** | `npm run dev`（`frontend/`） | 开发界面，`/api` 代理到 8000 | 只有开发时需要 |

> ⚠️ **最容易踩的坑就是第三个**。功能代码都写好了、测试也全绿，但只要没起 Outbox，
> 收件箱就是空的 —— 因为通知是**异步投递**的：任务引擎只负责写进
> `notification_outbox`，投递由这个服务做。

---

## 一键启动

```powershell
# 隔离数据目录 + 不外推手机（推荐用于演示/联调）
.\scripts\run-dev.ps1 -DataDir "$env:TEMP\pa-demo" -NoNtfy

# 只起后端三件套
.\scripts\run-dev.ps1 -NoFrontend
```

脚本会：检查 venv、检查端口、启动四个进程、把日志写到 `.run\*.log`、
等 API 就绪、打印面板地址。**按 `Ctrl+C` 会一并停掉所有子进程。**

窗口被强杀、进程成了孤儿时：

```powershell
.\scripts\run-dev.ps1 -Stop
```

参数：

| 参数 | 作用 |
| --- | --- |
| `-DataDir <路径>` | 数据目录。给了就用它（五个数据库全落那儿），**不碰项目根目录的真实数据** |
| `-ApiPort <端口>` | API 端口，默认 8000。改了要同步改 `frontend/vite.config.ts` 的代理 |
| `-NoNtfy` | 清空 `NTFY_TOPIC`：站长的通知会投递失败，而**不会真的推到手机** |
| `-NoFrontend` | 只起后端三件套 |
| `-Stop` | 停掉上次启动的进程（读 `.run\pids.txt`） |

---

## 手动启动

四个终端，都在仓库根目录（前端那个在 `frontend/`）：

```powershell
.\.venv\Scripts\python.exe -m uvicorn src.main:app --host 127.0.0.1 --port 8000
.\.venv\Scripts\python.exe -m src.scheduler
.\.venv\Scripts\python.exe -m src.core.outbox_service

cd frontend
npm run dev
```

然后浏览器打开 **http://127.0.0.1:5173**。

---

## 自检

```powershell
.\.venv\Scripts\python.exe scripts\check_services.py
```

它**不看进程列表，只看数据里的症状**（这才靠得住）：

```
Personal Agent - service self-check
==========================================================
  data dir   C:\Users\...\Temp\pa-demo
  tasks db   C:\Users\...\Temp\pa-demo\tasks.db
  api        OK        http://127.0.0.1:8000/health
  frontend   OK        http://127.0.0.1:5173
  outbox     OK        no pending rows
  scheduler  OK        no overdue tasks
  ntfy       NOT configured (owner notifications will fail, in-app still works)
  tasks      active 2 / paused 0 / archived 0
  inbox      2 pending in-app items
==========================================================
全部正常。
```

判定规则（都是**症状**，不是猜）：

| 检查 | 判据 | 说明 |
| --- | --- | --- |
| `outbox` | 有 `pending` 行且最老的超过 120 秒 | 投递服务没在跑 |
| `scheduler` | 有 `active` 任务的 `next_run_at` 超过 180 秒未处理 | 调度器没在跑 |
| `api` | `/health` 不可达 | API 没起来 |
| `frontend` | `5173` 不可达 | 只在开发时需要，不算故障 |

输出刻意用英文：本机 Windows 控制台是 GBK，Python 打中文会变乱码。

---

## 环境变量

`.env`（仓库根目录，已被 `.gitignore` 忽略）里需要这些键：

| 键 | 用途 |
| --- | --- |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL` | 聊天（DeepSeek 走 OpenAI 兼容接口） |
| `NTFY_SERVER` | 推送服务地址，默认 `https://ntfy.sh` |
| `NTFY_TOPIC` | 推送主题。**留空则站长的通知投递失败**（站内投递不受影响） |

进程级覆盖（不改文件）：

| 变量 | 作用 |
| --- | --- |
| `PERSONAL_AGENT_DATA_DIR` | 切换全部数据库的根目录 —— 演示/测试用的隔离开关 |
| `NTFY_TOPIC` | 设成空串即可安全联调（配合 `-NoNtfy`） |

---

## 通知到底发给谁

| 任务主人 | 渠道 | 在哪里看 |
| --- | --- | --- |
| 站长（`owner`） | ntfy | 手机 / ntfy 客户端 |
| 管理员、其他角色 | 站内收件箱 | 任务中心顶部的「收件箱」 |

口径的来历与取舍见 `docs/TASK_ENGINE.md` 第 8 节。

---

## 测试

```powershell
.\scripts\test.ps1 all        # 隔离审计 + 后端（受保护运行器）+ 前端构建 + Playwright
.\scripts\test.ps1 backend    # 只跑后端
.\scripts\test.ps1 frontend   # 只跑前端构建 + Playwright
```

后端用例跑在**受保护运行器**下：它会把 `PERSONAL_AGENT_DATA_DIR` 指到临时目录，
并拦截真实数据库与真实网络，所以**跑测试不会动你的数据**。

新增测试文件必须在 `tests/registry.py` 注册，然后重新生成分层文档：

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py list --doc
```

否则隔离审计会失败（它会比对 `tests/TEST_LAYERS.md` 里的摘要）。

---

## 已知缺口（诚实版）

- **前端 dist 没有挂到 FastAPI 上**：`src/main.py` 里没有 `StaticFiles`，
  所以后端进程不提供界面。开发用 Vite dev server；**生产怎么发前端，目前没有实现**
  （要么补一个静态挂载，要么前面放 nginx）。
- **三个后端进程都是手动常驻**，没有进程守护（systemd / nssm / 计划任务都没有配），
  线上要自己包一层。
- **提醒有两套并存**：旧的 `reminders` 表由 `scheduler.py` 直推 ntfy；
  新的 `reminder_requests` 表走按人投递。两条链路都在跑，尚未合并。
- **收件箱上限 50 条**，未做分页。
