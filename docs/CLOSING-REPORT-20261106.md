# 个人版精简 · 收尾报告（供 Kimi 复核）

> 日期：2026-11-06
> 基线：`main @ 3ced78e`（与 `origin/main` 同步）
> 依据：`PERSONAL-EDITION-TRIM-V2-20261106.md` 阶段 1–5
> 结论：**五阶段全部完成；关键不变量保留；CI 与本地全量均绿。**

---

## 0. 给复核人的结论

| 判定项 | 结论 |
|---|---|
| 不用的功能是否删掉 | ✅ 视频路线未恢复；邀请/多用户运行时删除；`model_policy` 删除；CLI 展示层删除 |
| 关键防线是否保留 | ✅ 审批、认证、幂等、取消、恢复、回收、容量、备份恢复全部保留并有测试 |
| 数据是否可恢复 | ✅ 备份→破坏→恢复→owner 登录演练通过 |
| 是否意外暴露 | ✅ 默认只绑 `127.0.0.1`，非回环需 `-AllowNetwork`；有监听检查脚本 |
| 展示型验收是否降频 | ✅ 长压测/对照/评估集/录屏改为手动，不进门禁 |
| 测试数量是否为删而删 | ❌ 没有按数量删；821 条 = 保留能力 + 删除功能专属测试 |

---

## 1. 五阶段执行结果

| 阶段 | 内容 | 提交 | 验收 | CI |
|---|---|---|---|---|
| 1 | 备份/恢复、CLI 只读、监听检查、绑定守卫 | `abd98f8`、`08f03ab` | 4 条备份测试（含恢复后登录）+ 2 条 CLI + 4 条 netcheck | `42148622603` |
| 2 | 删除未生效的 `model_policy` | `607e193` | 30 条路由相关测试；历史事件兼容测试 | `42149039323` |
| 3 | 删除邀请/多用户运行时代码 | `21b5d65` | 认证/聊天 25 条测试；净删 452 行 | `42151369919` |
| 4 | CLI 只留只读 JSON；冒烟按覆盖缺口决定 | `06597e7` | CLI 2 条（哈希不变、不创建库） | `42151653022` |
| 5 | 最终验收（全量/浏览器/残留/记录） | `3ced78e` | 821 用例、34/34 Playwright、残留 0 | `42153509719` |

---

## 2. 保留 vs 删除

### 保留（KEEP）

| 能力 | 证据 |
|---|---|
| 高危审批（确定性，不看置信度） | `tests/test_routing_policy.py`、`test_approval_gate.py` |
| 歧义澄清且不调模型 | `test_agent_stream.py` |
| 幂等 | `test_platform_idempotency.py` |
| 取消后进程树退出 | `test_platform_exec_cancel.py`、`test_tier1_runner.py` |
| 队列上限 / 429 / 终态 | `test_platform_queue.py` |
| 租约失效停接活 | `test_platform_scheduling.py` |
| 回收无残留 | `verify_w34_residue.py`（ok=true） |
| owner 登录 / 首启建号 | `test_auth_store.py`、`test_auth_bootstrap.py` |
| 注册/邀请入口关闭 | `test_single_user_surface.py` |
| 备份可恢复 | `test_backup_restore.py` |

### 删除（DELETE）

| 项 | 处理 | 影响 |
|---|---|---|
| 邀请码表/签发/邀请注册 | 删除运行时代码与 5 条专属测试 | 旧库中的 `auth_invites` 表不读取、不迁移 |
| `model_policy` | 字段、fallback 引用、文档、断言删除 | 历史事件仍可读（有兼容测试） |
| CLI 展示排版 | 删除，只留 `--json` | 前端控制台负责展示 |
| 视频路线 | 未恢复 | 历史证据文件保留，仅作记录 |

### 降为手动（MANUAL）

- 2/4/8/12 RPS 长压测（仅队列/调度/沙箱/资源控制变化后跑）
- 固定/弹性容量对照
- 完整路由评估集与混淆矩阵
- G3 录屏与对外展示材料

---

## 3. 关键数字

| 指标 | 值 |
|---|---|
| 后端全量 | **821 用例 PASS**（403.8 s） |
| 前端 | 构建通过；Playwright **34/34** |
| 测试索引审计 | PASS，0 violation |
| 备份恢复演练 | 4/4（含恢复后 owner 登录） |
| CLI 只读 | 运行前后库哈希一致；缺失库不被创建 |
| 监听检查 | 解析/回环判定 4/4 |
| 残留 | 目录/DB/进程 = 0/0/0 |
| CI | 五轮全绿（见 §1） |

---

## 4. 浏览器主链路证据

截图：`docs/evidence/stage5-acceptance.png`

- 最近路由决策：`data.analysis · conf 0.82`，risk=low，审批=false
- 节点：`node_local`，capacity=4，state=ready
- 最近执行：`succeeded · exit=0`
- 沙箱：`failed:1` —— 演示进程退出后对账把死沙箱标 `failed`，是预期行为

---

## 5. 未做与边界（请按说明项复核）

1. **未在浏览器聊天框发真实消息**：需要有效模型 Key；路由由单测、垂直切片与控制台事件验证。
2. **刷新/重启恢复**：由自动化测试覆盖，未在浏览器重复演练。
3. **长压测/弹性对照**：手动项，不随每次改动执行。
4. **分类器层**：继续暂缓（规则层 94.3% 已达标）。
5. **G3 视频**：纯展示，未录。

---

## 6. 复核步骤（约 10 分钟）

```powershell
cd "C:\Users\123\Personal Agent"
git pull

# 1) 全量（约 7 分钟）
.\.venv\Scripts\python.exe scripts\run_guarded_tests.py backend

# 2) 备份 → 恢复演练
.\.venv\Scripts\python.exe -m unittest tests.integration.test_backup_restore -v

# 3) 邀请/注册入口确实关闭
.\.venv\Scripts\python.exe -m unittest tests.integration.test_single_user_surface -v

# 4) CLI 只读
.\.venv\Scripts\python.exe -m unittest tests.integration.test_console_cli -v

# 5) 监听暴露面（应只看到 127.0.0.1）
.\.venv\Scripts\python.exe scripts\check_listeners.py --port 8000

# 6) 前端口径
cd frontend; npm run build; npx playwright test
```

---

## 7. 变更清单（本轮 5 阶段）

```
3ced78e test: 阶段5 验收（浏览器链路 + 记录）
06597e7 refactor: CLI 控制台只保留只读 JSON 输出（阶段4）
21b5d65 refactor: 删除邀请与多用户运行时代码（阶段3）
607e193 refactor: 移除未生效的 model_policy（阶段2）
08f03ab docs: 阶段1 落地记录；测试补充恢复后登录校验
b29fc10 test: platform_api 临时目录清理容错
abd98f8 feat: 备份恢复、CLI 只读、监听地址检查与绑定守卫（阶段1）
```

---

## 8. 日常使用建议（测试分三层）

| 层 | 何时 | 命令 |
|---|---|---|
| 日常 | 改完一个模块 | 该模块测试 + `demo_route_pipeline.py`（约 30 s） |
| 阶段 | 合并/交付前 | 全量后端 + 前端构建 + Playwright（约 9 min） |
| 手动 | 相关实现变化 | 长压测、弹性对照、完整评估集、G3 |
