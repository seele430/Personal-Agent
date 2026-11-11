# Changelog

## v1.3.2 — 2026-11-11

### 移除

- 前端撤下「Bot 管理」入口、`BotsView` 组件与「我的 BOT」侧栏区块；
- `bot_api` 路由与 `bots.db` 数据保留，仅停止 UI 入口，见 `docs/DEPRECATED-bots.md`。

### 验证

```text
前端 build：exit 0
Playwright：auth/activity/artifacts/memory 共 18 passed
```

## v1.3.1 — 2026-11-11

### 修复

- 补齐 `/api/console`、`/api/monitors`、`/api/notifications`、`/api/inbox`、
  `/api/teams`、`/api/usage`、`/api/bots` 的前缀兼容重写；修复 v1.3.0 中
  平台控制台、任务中心等页面返回 404。
- 新增路由兼容回归测试。

### 验证

```text
后端：collected 2231 / ran 2225 / passed 2223 / skipped 2 / not-run 6
```

## v1.3.0 — 2026-11-11

单用户、本地运行的 Personal Agent 1.3 版本。数据默认不出本机，服务只监听
`127.0.0.1`。

### 新增

- **模型 fallback**：主模型失败时按配置依次尝试备用模型，记录实际使用模型；
- **QQ 通知**：外部任务完成/失败可走 QQ 出站通知，支持真机发送；
- **Office 转换**：docx / xlsx / pptx → PDF，owner 绑定的 Artifact 输入，
  Tier1 隔离执行、120 s 超时、PDF 结构与页数校验、Artifact 登记与预览；
- **记忆闭环**：
  - 聊天工具 `memory_remember`（ask 审批）/ `memory_recall`；
  - 同 key 原子更新，新版本 `supersedes_id` 指向旧条；
  - 当前事实折叠、历史链（旧→新）、成环/分叉/断链/跨 owner 检测；
  - API：列表/搜索/详情/历史/软删除；
  - 面板：列表/搜索/分页/详情/历史/编辑/软删除；
  - 浏览器审批 E2E（mock 模型 + 真实后端/审批/runner/SQLite）；
- **兼容修复**：`/api/auth`、`/api/chat`、`/api/reminders` 前缀与前端调用对齐，
  修复网页登录、聊天、提醒的 404。

### 变更

- 记忆写入的 trust 由服务端派生，模型不能传 owner/trust/supersedes；
- 删除记忆为**整条版本链软删**，历史保留，非安全擦除；
- README 测试计数更新为当前基线。

### 移除

- **Sleep-time Compute**（Phase 1/2）：完成实验后判定无价值，功能整体删除；
  仅保留结论文档与实验数据，不再提供代码/API/面板。

### 验证基线

```text
后端：collected 2230 / ran 2224 / passed 2222 / skipped 2 / not-run 6
Playwright：79
前端构建：tsc -b && vite build，exit 0
```

### 已知边界

- 单用户、单 worker；多进程写记忆依赖 SQLite `BEGIN IMMEDIATE`，无独立写队列；
- v1.0 的 W6 过程偏差、v1.1 长音频 93.3%、v1.2 的 LAN/手机/iOS 未验，
  均按历史记录保留，不在本版本宣称完成；
- 记忆软删可读、无恢复入口，不是安全擦除；
- 远程 provider 场景下，recall 文本会进入模型上下文并外发；
- 浏览器审批 E2E 使用 mock 模型，不宣称真实模型执行质量。

### 升级说明

- 无数据库迁移脚本；首次启动按现有 `PERSONAL_AGENT_DATA_DIR` 使用数据目录；
- 建议升级前备份 `knowledge.db`、`auth_users.db`、`artifacts/`；
- 本版本不包含 Sleep 相关表/路由，旧实验数据可自行清理。
