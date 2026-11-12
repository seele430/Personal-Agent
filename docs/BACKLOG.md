# 需求留档（Backlog）

> 用途：不是计划、也不是承诺，只记“被提过但暂不排期”的需求。每条含来源、原话、现状、候选方案、决策、下一步。
> 与 `docs/STATUS-20261107.md` 的区别：STATUS 记“做过什么”，这里记“还没做什么”。

---

## 2026-11-12 · 桌面自动化：打开应用 / 播放控制

- **来源**：owner 通过 QQ 小号 `3788260717` 发给 PA
- **原话**：「你能帮我打开网易云音乐并且点击播放吗」（`message_id=2016911235`，`data/qq-audit.jsonl` 有 `notice-sent`）
- **PA 现状**：`intent=unknown` → `policy_decision=clarify`，回「请再说清楚一点：可以问问题，或用 /status、/ask、/cancel。」——不执行，也没有说明“做不到”。

### 为什么做不到

设计稿把 **GUI 自动化 / 虚拟鼠标 / 点击** 写在「不做什么」里；工具表里没有任何桌面控制能力。所以这不是 bug，是能力边界，但**回退文案没把边界说清楚**，用户体感是“它听不懂”。

### 候选方案（按成本从低到高）

| # | 方案 | 覆盖 | 成本 | 备注 |
|---|---|---|---|---|
| 1 | **零依赖**：`open_app`（`subprocess` / `os.startfile`）+ 媒体键 `VK_MEDIA_PLAY_PAUSE`（0xB3）+ 进程/窗口查询 | 打开/关闭常见软件、播放暂停、切歌 | 约 20 行，无新依赖 | 推荐先做；网易云注册了系统媒体控制，不依赖坐标和分辨率 |
| 2 | `pywinauto`（UIA） | 按控件名点按钮、填输入框、后台窗口 | 新增依赖 | Electron 应用（网易云）可访问性树时好时坏 |
| 3 | [Windows-MCP](https://github.com/CursorTouch/Windows-MCP)（MIT） | 通用桌面操作（Click/Type/App/Snapshot/PowerShell） | 需 **Python 3.14+** 做 sidecar；PA 需加 MCP 客户端 | 不依赖视觉模型，走 UI Automation |
| 4 | [Agent S](https://github.com/simular-ai/Agent-S) / [Microsoft UFO](https://github.com/microsoft/UFO) | 通用 computer-use 智能体 | 要 VLM / 研究级框架 | 本机 6 GB 显存跑不动，得调云 API，性价比低 |

### 决策

- **2026-11-12 暂不实现**：owner 要求先留档（`「这个需求记住留档吧」`）。
- 真要做时，**先改设计稿**：把「不做 GUI 自动化」从非目标里拿掉或写清边界，再按方案 1 出计划文件（新增 `src/tools/desktop.py` + 工具注册 + 测试），owner 批准后实施。

### 已知风险

- 媒体键依赖应用注册了系统媒体控制；多个播放器同时运行时可能抢焦点/控制错对象。
- 后台窗口操作超出方案 1 的能力，需要方案 2/3。
- 任何桌面自动化都会让“单用户本地、数据不出本机”的边界变复杂：工具能点任何窗口，包括敏感界面。

### 关联

- 相关文档：`docs/AGENT_TOOLS.md`（工具清单）、`docs/threat-model.md`（边界）
- 相关会话：2026-11-12 复审循环 M1/M2 落地当天提出
