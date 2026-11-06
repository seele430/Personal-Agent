# 阶段 5 验收记录（个人版精简）

> 日期：2026-11-06
> 基线：`main @ 06597e7` 之后
> 范围：V2 §9 阶段 5「最终验收与交接」

---

## 1. 自动化门禁

| 项 | 结果 | 证据 |
|---|---|---|
| 后端全量 | **821 用例 PASS**（403.8 s） | `scripts/run_guarded_tests.py backend` |
| 前端构建 | 通过 | `npm run build` |
| Playwright | **34/34 通过**（1.4 min） | `npx playwright test` |
| 测试索引审计 | PASS，0 violation | `scripts/run_tests.py audit` |
| CI | 三 job success | run `42151653022`（阶段 3/4） |

## 2. 真实浏览器主链路

步骤：建数据目录 → 跑 `demo_route_pipeline.py`（路由 → 沙箱 → 图表 → 轨迹）→ 启动服务 → 浏览器登录 → 打开「平台」。

结果（截图 `docs/evidence/stage5-acceptance.png`）：

| 区块 | 内容 |
|---|---|
| 最近路由决策 | `data.analysis · conf 0.82`，risk=low，审批=false |
| 节点 | `node_local`，capacity=4，state=ready |
| 最近执行 | `exec_...`，`succeeded · exit=0` |
| 沙箱 | `failed:1` |

关于 `failed:1`：演示进程退出后，服务启动时的对账发现该沙箱的 runner 已不存在，
按设计将其标记为 `failed`（避免把死沙箱当活的使用）。这正好验证了对账逻辑，
不是链路失败——对应的 exec 是 `succeeded`。

## 3. 关键行为不变量（自动化覆盖）

| 不变量 | 覆盖测试 |
|---|---|
| 高危审批（确定性，不看置信度） | `tests/test_routing_policy.py`、`tests/integration/test_approval_gate.py` |
| 歧义澄清不调模型 | `tests/test_agent_stream.py::test_ambiguous_message_asks_clarification_without_model_call` |
| 幂等 | `tests/integration/test_platform_idempotency.py` |
| 取消后进程树退出 | `tests/integration/test_platform_exec_cancel.py`、`test_tier1_runner.py` |
| 队列上限 / 429 / 终态 | `tests/integration/test_platform_queue.py` |
| 租约失效不接活 | `tests/integration/test_platform_scheduling.py::test_expired_lease_stops_accepting` |
| 回收无残留 | `scripts/verify_w34_residue.py`（本次 ok=true） |
| owner 登录 / 首启建号 | `tests/test_auth_store.py`、`tests/integration/test_auth_bootstrap.py` |
| 注册/邀请入口关闭 | `tests/integration/test_single_user_surface.py` |
| 备份可恢复 | `tests/integration/test_backup_restore.py` |

## 4. 本次验收执行过的命令

```powershell
# 后端全量
.\.venv\Scripts\python.exe scripts\run_guarded_tests.py backend

# 前端
cd frontend; npm run build; npx playwright test

# 残留
.\.venv\Scripts\python.exe scripts\verify_w34_residue.py

# 浏览器链路
# 1) 准备数据目录并跑垂直切片
.\.venv\Scripts\python.exe scripts\demo_route_pipeline.py --data-dir "$env:TEMP\pa-stage5-final"
# 2) 建 owner
.\.venv\Scripts\python.exe -c "from src.core.auth_store import AuthStore; AuthStore(r'$env:TEMP\pa-stage5-final\auth_users.db').bootstrap_owner('seele','stage5-accept-2026')"
# 3) 起服务（绑定 127.0.0.1）
.\.venv\Scripts\python.exe -m uvicorn src.main:app --host 127.0.0.1 --port 8142
# 4) Playwright
node scripts/e2e_stage5.cjs
```

## 5. 边界与未覆盖项

1. **未通过聊天框发真实消息**：需要有效模型 Key；路由已在单测/集成测试与垂直切片验证。
2. **刷新恢复 / 重启恢复**：由 `agent_stream` 与恢复相关测试覆盖，未在浏览器中重复演练。
3. **长压测 / 弹性对照**：不属于每次验收项，按需手动执行（见 `docs/bench/ELASTIC.md`）。

## 6. 结论

阶段 5 的自动化门禁与真实浏览器链路均通过；删除项（视频、邀请码、`model_policy`、CLI 展示）
没有带走关键不变量。个人版可进入日常使用与按需手动验收。
