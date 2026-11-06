# 关键行为组（红线测试清单）

> 日期：2026-11-06
> 用途：个人版精简后，明确「删功能时不能带走」的检查；验收以**测试实际通过**为准，不以文件名存在为准。
> 用法：每次删除/重构后，至少运行本清单里的文件；阶段门禁再加全量。

| # | 不变量 | 测试文件 / 脚本 | 说明 |
|---|---|---|---|
| 1 | 高危操作必须审批，且不看置信度 | `tests/test_routing_policy.py`、`tests/integration/test_approval_gate.py` | 确定性闸门 |
| 2 | 歧义请求澄清且不调模型 | `tests/test_agent_stream.py::test_ambiguous_message_asks_clarification_without_model_call` | 防误执行 |
| 3 | 请求幂等 | `tests/integration/test_platform_idempotency.py` | 重试不重复执行 |
| 4 | 取消后进程树退出 | `tests/integration/test_platform_exec_cancel.py`、`tests/integration/test_tier1_runner.py` | Job Object |
| 5 | 队列上限 / 429 / 终态可读 | `tests/integration/test_platform_queue.py` | 防资源耗尽 |
| 6 | 节点租约失效停止接活 | `tests/integration/test_platform_scheduling.py::test_expired_lease_stops_accepting` | 不派给死节点 |
| 7 | TTL/销毁后无残留 | `scripts/verify_w34_residue.py`（手动/阶段） | 进程/目录/DB |
| 8 | owner 登录与首启建号 | `tests/test_auth_store.py`、`tests/integration/test_auth_bootstrap.py` | 单用户认证 |
| 9 | 注册/邀请入口关闭 | `tests/integration/test_single_user_surface.py` | 防路由被加回 |
| 10 | 备份逐文件可校验、可恢复启动 | `tests/integration/test_backup_restore.py` | 文件哈希 + 全应用启动 |
| 11 | CLI 只读离线 | `tests/integration/test_console_cli.py` | 哈希不变 |
| 12 | 默认只绑回环 | `tests/test_netcheck.py`、`run-dev.ps1` 守卫 | 防局域网暴露 |

## 删除功能时的机械检查

1. 删除功能与专属测试必须在同一提交；
2. 删完运行上表 1–10 中受影响项 + 全量；
3. 若某个红线测试文件被删，必须在 PR/提交说明里写清替代检查。
