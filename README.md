# Personal Agent

> 一个跑在本地的个人 AI 工作台：聊天、提醒、定时任务、额度账本、产物管理与预览、
> 活动时间线、数据生命周期，全部在一台笔记本上完成，**数据不出本机**。

---

## 这是什么

一个**单人自用**的本地工作台，把日常重复的事交给它，并且**每一步都留痕、可核对**：

| 你能做的 | 它怎么保证可信 |
|---|---|
| 和模型聊天、让它调工具 | 工具调用有去重与次数上限，执行留轨迹 |
| 让它定时提醒、跑任务 | 调度器在工作进程外，投递结果与失败都落库 |
| 把产物交给它管理 | 产物有 SHA-256、可预览、可下载、可对比两个版本 |
| 回看"今天都发生了什么" | 活动时间线：产物 / 执行 / 模型调用统一成只读事件 |
| 清理旧数据 | 数据生命周期分 DB 行与文件两类，**先预演、再确认** |
| 用手机看一眼 | 远程**只读**访问：白名单 + 自签 TLS，写方法一律拒绝 |

另外有两条**本地 AI 管线**：语音转写（faster-whisper，CPU）与图生图（ComfyUI，低显存模式）。

### 部署形态：单用户

本项目按**个人自用**设计：一台机器、一个站长账号。

- 首次启动：登录页变成「创建站长账号」，之后不再有注册入口；
- 不暴露邀请码 HTTP 接口（`/auth/register`、`/auth/invite` 均未注册路由）；
- 邀请码表与相关运行时代码已删除；旧库里的历史表不读取、不迁移；
- 对外暴露时配置 `PERSONAL_AGENT_PLATFORM_TOKEN`；若走反向代理，需让代理改写来源并显式信任它（见 `docs/platform-contract.md`）。

---

## 演示

**[▶ 观看演示视频（2 分 47 秒）](docs/Personal-Agent-Demo.mp4)**

> 视频录制较早，界面与部分功能可能与当前版本不一致；以本 README 与 `docs/STATUS-20261107.md` 为准。

---

## 功能一览

| 模块 | 说明 |
|---|---|
| **聊天** | 流式对话、工具调用（提醒 / 沙箱读写 / 搜索 / 图生图），调用留轨迹 |
| **Bot 管理** | 自建 Bot（行为指令 / 头像 / 音色），上限 3 个 |
| **语音** | Edge TTS 神经网络语音，每个 Bot 独立音色 |
| **提醒** | 定时提醒 + 应用内推送 + 邮件/推送通道 |
| **任务中心** | 定时任务、执行历史、健康检查 |
| **额度** | 模型调用计费、预算控制、用量报表 |
| **监控** | 站点健康检查 + 告警分发 |
| **产物** | 登记、预览（文本/图片/PDF）、下载、**两版对比** |
| **活动时间线** | 产物 / 执行 / 模型调用汇总成只读事件流（API + CLI + 面板） |
| **数据生命周期** | DB 行与文件分别清理，预演 + 确认双开关 |
| **视频摘要** | 探测 / 音轨 / 转写 / 要点采样 / 关键帧 / 截断标记 |
| **图生图** | ComfyUI 工具化：显存预检、步数上限、单张、用完即停 |
| **远程只读** | 白名单 + 自签 TLS + 一次性链接换 cookie；手机已验证 |
| **外部模型工具** | `external_task`：调用外部模型 API 写文本产物（`policy=ask`，联网花钱需确认） |
| **平台控制台** | 总览 / 节点 / 沙箱 / 执行 / 弹性五个区；建沙箱、提交执行、取消、销毁、cordon / drain |
| **弹性** | 期望容量控制器（稳定期 + 冷却期）、扩缩容事件落库、15 分钟趋势采样 |
| **演示与模拟** | What-if 模拟（只算不改）、按持久事件回放真实扩缩容 |

### 平台控制台的一条底线：数字不能冒充结果

控制台的展示口径写死在实现里，不靠文案提醒：

- **202 只是接收**，不等于执行成功；成功率按终态算，取消单列、不进分母；
- **决策不等于扩容**：`hold`（等待稳定期 / 冷却中）是决策，只有容量真变了才写事件；
- **实际容量按租约**（`accepting`）算，不按节点行数算 —— 真机演练里 2 个节点全是过期租约，实际容量是 0；
- 三态严格分开：**实时 / 采样（进程内 15 分钟环缓冲，重启即丢）/ 仿真（What-if、回放）**；
- 未接控制器时显示「已计算，未驱动真实容量」，不把计算值说成扩容。

---

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.12 · FastAPI · SQLite · APScheduler |
| 模型 | DeepSeek Chat（`deepseek-chat` / `deepseek-flash`） |
| 语音 | Edge TTS（微软神经网络语音，免 key）；转写用 faster-whisper（CPU） |
| 图像 | ComfyUI（本机，SD 1.5，低显存模式） |
| 前端 | React 19 · TypeScript · Vite 8 |
| 测试 | unittest（**2180 个后端用例**）+ Playwright（**79 条前端链路**） |

---

## 核心设计：为什么是这四件事

### ① 远程访问只读，且"只读"是代码保证的

手机要看产物，但不能因此把整个服务暴露。做法是三层，**任何一层都不靠文档提醒**：

1. **只读守卫**（ASGI 中间件）：先鉴权 → 再判方法 → 再判白名单；
   白名单只有 `/api/artifacts`、`/api/activity`、`/health`，`POST/PUT/PATCH/DELETE` 一律 403；
2. **手机可登录**：浏览器发不了 `Authorization` 头，所以支持 `?token=` 一次性链接
   → 303 + `Set-Cookie: pa_remote`（`HttpOnly`、`SameSite=Strict`、HTTPS 加 `Secure`、
   `Referrer-Policy: no-referrer`）；令牌只在 query 里出现一次，随后被重定向掉；
3. **失败有代价**：连续 10 次失败封 300 秒（429 + `Retry-After`）；访问日志剥掉查询串，
   令牌不进日志，1 MB × 3 份自动轮转。

真机验证过：Android 15 + 手机热点，`/api/activity/timeline` 200、`/api/chat` 403；
验完立即停服务并**轮换令牌**。详见 `docs/threat-model-remote.md`。

### ② 测试必须被"关起来"跑

测试写真实库、偷偷联网，是这类项目最常见的静默事故。所以：

- **受保护运行器**（`scripts/test.ps1` → `scripts/run_tests.py`）：SQLite 路径重定向到临时目录，
  socket 连接被守卫拦截，越界直接抛 `TestSafetyViolation`；
- **隔离审计**（`run_tests.py audit`）：静态扫描每个测试文件的 schema（层 / 库 / 网络 / 风险），
  与声明不符就失败；
- 代价是诚实的：6 条 mock provider 用例在守卫下会 skip（回环连接被拦），
  所以它们**另外单独运行**并留日志，不把"2180 通过 + 2 skip + 6 not-run"写成"2188 全过"。

### ③ 数据生命周期：先预演，再确认

删数据是不可逆动作，所以两类清理都做成"两步"：

- **DB 行**：`retention.py` 按天清理 spans / 平台执行记录，默认只出计划；
- **文件**：`lifecycle/files.py` 按后缀白名单 + 修改时间清理报告/日志/关键帧，
  跳过符号链接，默认也是预演；
- 真正删除必须显式 `confirm=True`；CLI 里 `--apply` 不带 `--confirm` 直接拒绝（退出码 2）。

### ④ GPU 只有一条路：低显存 + 硬上限

2026-11-08 这台 6 GB 显存的机器在 ComfyUI 出图 + 视频转写同跑时触发
`0x116 VIDEO_TDR_FAILURE` 蓝屏，20 分钟才恢复。之后把四条写死：

1. 只用 `comfy-agent/scripts/start-comfyui-safe.ps1` 启动（先查空闲显存 ≥3 GB、
   `--lowvram --reserve-vram 1`、用完 `-Stop`）；
2. 采样步数 ≤30；输入图任一边 >768 先缩到 512；
3. 调用前读 `/system_stats`，空闲显存 <2500 MB 直接中文拒绝，**不发请求**；
4. 单张、不连跑、超时 120 秒。

自检脚本 `scripts/verify_gpu_guard.py` 会检查这四条有没有被改掉（改小常量、删参数都会红）。
详见 `docs/gpu-protection.md`。

---

## 快速开始

```powershell
# 1. 依赖
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

cd frontend
npm install
cd ..

# 2. 配置（需要 DeepSeek API Key）
#    在 .env 里写：DEEPSEEK_API_KEY=sk-xxxx

# 3. 一键启动（api:8000 / scheduler / outbox / frontend:5173）
.\scripts\run-dev.ps1

# 停止
.\scripts\run-dev.ps1 -Stop
```

打开 <http://127.0.0.1:5173>。

---

## 项目结构

```
Personal Agent/
├── src/
│   ├── agent.py            # 模型调用与工具分发
│   ├── main.py             # FastAPI 入口（含 /api 路由与远程守卫装配）
│   ├── artifacts_api.py    # 产物：列表 / 预览 / 下载 / Diff
│   ├── activity_api.py     # 活动时间线
│   ├── video_api.py        # 视频总结（受控同步接口）
│   ├── tools_api.py        # 图生图等工具端点
│   └── core/ · artifacts/ · activity/ · diff/ · lifecycle/ · video/
│       · remote/ · collab/ · platform/ · tools/ · knowledge/ · cost/ · ...
├── frontend/src/
│   ├── App.tsx             # 外壳与导航
│   ├── chat/               # 聊天视图与消息组件
│   ├── components/         # 产物侧栏 / 活动时间线 / 视频 / 图生图面板
│   ├── BotsView.tsx        # Bot 管理
│   └── PlatformView.tsx    # 平台控制台
├── tests/                  # 测试文件清单见 tests/registry.py
├── scripts/                # 起停、测试、验收、运维脚本
└── docs/                   # 架构 / 威胁模型 / GPU 保护 / 验收报告 / archive
```

---

## 测试

```powershell
.\scripts\test.ps1 backend
```

（后端测试统一走受保护运行器：SQLite / socket 守卫 + 独立临时数据目录；`scripts/run_tests.py <layer>` 可单独跑某一层。）

**2180 个用例**（收集 2188，2 条守卫 skip，6 条 mock 用例另行运行），覆盖：认证与权限、限流与审计、
额度控制、任务调度、提醒投递、产物登记与预览、Diff、活动时间线、数据生命周期、
视频管线（probe/音轨/转写/要点采样/关键帧/截断标记）、远程守卫与桥接、协作注册表与验收、
平台侧的沙箱 / 执行 / 队列 / 调度 / 弹性控制器 / What-if 与回放。

前端 Playwright 79 条（含平台控制台、产物预览与对比、活动时间线、视频总结、图生图面板）：

```powershell
cd frontend
npx playwright test                       # 全量
npx playwright test tests/console.spec.ts # 只看平台控制台
```

### 常用命令

```powershell
# 后端全量（受保护运行器：SQLite / socket 守卫 + 临时数据目录）
.\scripts\test.ps1 backend

# 测试隔离审计（静态扫描，别跳过）
.\.venv\Scripts\python.exe scripts\run_tests.py audit

# 端到端：黄金路径 10 步 + 远程 TLS 8 项 + 协作闭环
.\.venv\Scripts\python.exe scripts\verify_e2e.py

# 混合回归（随机顺序 10 轮，含 GPU 保护断言）
.\.venv\Scripts\python.exe scripts\run_mixed_regression.py --rounds 10

# GPU 保护自检 / 文档一致性自检
.\.venv\Scripts\python.exe scripts\verify_gpu_guard.py
.\.venv\Scripts\python.exe scripts\check_docs_consistency.py

# 视频：摘要（默认 faster-whisper CPU 后端）
.\.venv\Scripts\python.exe scripts\summarize_video.py <视频> --backend faster --model-dir <模型目录>

# 图生图：一次一张，安全启动 → 出图 → 必定停止
.\scripts\comfy_once.ps1 -Prompt "a cat on a chair" -Image input.png -Steps 8
```

### 视频转写：精度档位（2026-11-08 实测）

同一段 60 句 / 238.8 s 合成素材，faster-whisper **CPU int8**，按关键词计命中：

| 模型 | 命中 | 命中率 | 耗时 |
|---|---|---|---|
| `faster-whisper-medium`（默认） | 52/60 | **86.7%** | ~10 min |
| `faster-whisper-large-v3` | 56/60 | **93.3%** | 11.4 min |

- 短素材（20 句）是 **95%**，已通过；**长素材 93.3%，仍未达 95%，记为 partial**；
- **不要开 `--prompt` 术语偏置**：三次实测都变差（medium 52→51、large-v3 56→48、80%）；
- 关 VAD、把 beam 调到 10 都无收益（56→56，耗时 +45%）；
- large-v3 内存占用比 medium 更高（未精确量化），16 GB 机器上留意。
- **已知上限**：长音频（4 分钟以上）逐句转写在本机达到 **86–93%**，达不到 95%；错误以同音字为主。详见 [`docs/video-asr-limits.md`](docs/video-asr-limits.md)。

精度优先时：

```powershell
.\.venv\Scripts\python.exe scripts\summarize_video.py <视频> --backend faster --model-dir E:\AppData\models\faster-whisper-large-v3
```

> **GPU 保护**：本机 6 GB 显存，2026-11-08 曾因 ComfyUI + 视频转写同跑触发
> `0x116 VIDEO_TDR_FAILURE` 蓝屏。之后定为硬约束：ComfyUI 只能用
> `comfy-agent\scripts\start-comfyui-safe.ps1` 启动（显存 ≥3 GB、`--lowvram --reserve-vram 1`），
> `steps ≤ 30`、`vram_free < 2500 MB` 直接拒绝、`batch = 1`、用完即停。
> 视频转写固定 `device=cpu`、ffmpeg 不开 hwaccel。
> 细节与自检清单见 [`docs/gpu-protection.md`](docs/gpu-protection.md)。

前端有一个 Playwright 自测（验证语音的播放队列不被 stop 卡死）：

```powershell
$env:NODE_PATH = "frontend\node_modules"
node scripts\test-tts.cjs
```

---

## 安全修复记录

### 2026-11-07：`/v1` 限流按调用方分桶

- **影响面**：配置了 `PERSONAL_AGENT_PLATFORM_TOKEN` 的部署。未配置令牌时（本机模式）按来源主机分桶，不受影响。
- **问题**：限流桶的键取的是服务端自己配的令牌，所有调用方共用一个桶；未认证请求也会消耗额度，持续打满即可让持正确令牌的客户端收到 429。同一处代码还有两个小口子：非 ASCII 的 `Authorization` 会触发 500；`/v1/audit` 的免限流对匿名请求也生效。
- **修复**：键改为 `tok:<出示令牌指纹>`（已认证）/ `anon:<来源主机>`（未认证）；中间件仍为「限流 → 认证」，失败认证继续受约束；令牌比较改 UTF-8 字节；`/v1/audit` 免限流仅对已认证生效；桶字典加上限，且只回收「已满且闲置」的桶。
- **验证**：`tests/integration/test_platform_audit.py` 20 条用例；全量 885 用例 PASS。
- **致谢**：感谢 Matt（[@mattabate](https://github.com/mattabate)）私下报告该问题。

### 2026-11-07：未配置令牌时 `/v1` 写请求默认拒绝

- **影响面**：没有配置 `PERSONAL_AGENT_PLATFORM_TOKEN` 的本机部署。
- **问题**：未配置时 `/v1` 完全无认证。只监听 `127.0.0.1` 挡得住外部网络，却挡不住本机上的其它进程——任何本地程序都能创建沙箱并提交命令。
- **修复**：未配置令牌时，`POST` / `PUT` / `PATCH` / `DELETE` 一律 401 `UNAUTHORIZED`（错误消息提示开关名）；`GET` 维持旧行为，只读监控不受影响。确实需要匿名写时显式设 `PERSONAL_AGENT_ALLOW_ANON_V1=1`，启动日志会打出「允许匿名访问」警告。控制台不受影响，它走用户会话与 owner 校验。
- **验证**：`tests/integration/test_platform_auth.py` 10 条；平台与控制台 134 条；全量 **916 用例 PASS**。

细节见 `docs/journal.md` 的 D26.2 与 `docs/platform-contract.md`。

---

## 已知问题

| 问题 | 状态 |
|---|---|
| **长音频转写达不到 95%** | 🟡 已定案为已知限制：本机 CPU + large-v3 上限 86–93%，错误以同音字为主；详见 `docs/video-asr-limits.md`。 |
| **远程反向代理部署未验证** | 🟡 真机验证走的是 uvicorn 直接 TLS；nginx/caddy 反代、iOS 证书流程、弱网重连未覆盖。 |
| **6 条 mock 用例在守卫下 skip** | 🟡 不是测试失效：守卫阻断回环连接，这 6 条单独运行并通过，日志另存。 |
| 场景回放只含扩缩容事件 | 🟡 设计取舍：`hold` 决策不落库（30 秒一次 tick 会变成一天几千行），需要时单开决策表 + 保留期。 |
| 控制台提交的命令按空格切分 | 🟡 P1 不解析引号/转义；含空格参数的复杂命令仍需走 `/v1`。 |
| 全量测试偶发 1 例失败 | 🟡 2026-11-07 观察到 1 次（当次 916 例中 1 失败，用例名未及捕获），其后连跑多次全过，未复现。失败时运行器会**自动**把完整用例名与 traceback 落盘到 `.run/test-failure-<时间>-<层>.log` 并打印路径。 |

---

## 设计取舍记录

- **不做移动端页面**：手机只通过只读 API 看产物；写操作一律回到桌面端。
- **不做流式**：摘要与要点要求完整结构，流式反而割裂。
- **不把 LLM 纠错算进转写准确率**：那是"猜"，只能作为可读性增强，且必须标注。
- **语音用 Edge TTS 而非云端付费 TTS**：神经网络音质已够用，且完全免费。
- **删除历史功能时保留归档**：`docs/archive/` 存放已移除功能的设计文档，不删除历史。
