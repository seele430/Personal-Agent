# 测试分层索引（TEST_LAYERS.md）

> 本文件由 `python scripts/run_tests.py list --doc` 生成，请勿手工编辑。
> 校验：`python scripts/run_tests.py audit` 会比对摘要，文档过期将报 FAIL。

## 层与物理位置的关系

- **层归属由 `tests/registry.py` 的 Schema 决定，与文件所在目录无关。**
- 因此 `tests/` 根目录下平铺的测试同样属于某一层；迁移文件位置不改变层归属。
- 目录约定只作为未登记文件的兜底：`unit/`、`integration/`、`api/`，其余归 `legacy`。

## 检索命令

```
python scripts/run_tests.py list              # 列出全部层
python scripts/run_tests.py list integration  # 只列某一层
python scripts/run_tests.py integration       # 运行该层
```

<!-- membership-digest: 3e736edf02de2247 -->

## unit — L1 单元测试 (7 files)

| File | 物理位置 | Domain | DB | Network | Risk | Status |
|---|---|---|---|---|---|---|
| `test_agent_bounds.py` | tests/ 根 | agent | none | none | low | registered-inferred |
| `test_agent_call_count.py` | tests/ 根 | agent | none | none | low | registered-inferred |
| `test_agent_usage.py` | tests/ 根 | agent | none | none | low | registered-inferred |
| `test_rss_errors.py` | tests/ 根 | rss | none | mocked | medium | registered-inferred |
| `unit/test_agent_tool_limits.py` | tests/unit/ | reminder | none | mocked | medium | registered |
| `unit/test_reminder_eval.py` | tests/unit/ | eval | none | none | low | registered-inferred |
| `unit/test_url_policy.py` | tests/unit/ | rss | none | mocked | high | registered-inferred |

## integration — L2 数据集成测试 (26 files)

| File | 物理位置 | Domain | DB | Network | Risk | Status |
|---|---|---|---|---|---|---|
| `integration/test_guarded_asyncio.py` | tests/integration/ | test_infra | none | none | low | registered-inferred |
| `integration/test_reminder_store.py` | tests/integration/ | reminder | temporary_sqlite | none | high | registered |
| `integration/test_test_safety.py` | tests/integration/ | test_infra | temporary_sqlite | none | medium | registered-inferred |
| `test_access_policy_dispatcher.py` | tests/ 根 | access_policy | temporary_sqlite | mocked | medium | registered-inferred |
| `test_admin_alerts.py` | tests/ 根 | alerting | temporary_sqlite | none | medium | registered-inferred |
| `test_agent_usage_trace.py` | tests/ 根 | agent | temporary_sqlite | mocked | high | registered-inferred |
| `test_ai_budget.py` | tests/ 根 | billing | temporary_sqlite | none | high | registered |
| `test_ai_budget_reconcile.py` | tests/ 根 | billing | temporary_sqlite | none | high | registered |
| `test_ai_pricing.py` | tests/ 根 | billing | temporary_sqlite | none | high | registered-inferred |
| `test_ai_usage.py` | tests/ 根 | billing | temporary_sqlite | none | high | registered-inferred |
| `test_ai_usage_trace.py` | tests/ 根 | billing | temporary_sqlite | none | high | registered-inferred |
| `test_auth_store.py` | tests/ 根 | auth | temporary_sqlite | none | high | registered-inferred |
| `test_bootstrap_owner.py` | tests/ 根 | auth | temporary_sqlite | none | high | registered-inferred |
| `test_chat_admission.py` | tests/ 根 | chat | temporary_sqlite | none | medium | registered-inferred |
| `test_chat_db_restart.py` | tests/ 根 | chat | temporary_sqlite | none | medium | registered-inferred |
| `test_chat_ownership.py` | tests/ 根 | chat | temporary_sqlite | none | medium | registered-inferred |
| `test_chat_quota.py` | tests/ 根 | chat | temporary_sqlite | none | medium | registered-inferred |
| `test_dispatcher_history.py` | tests/ 根 | dispatcher | temporary_sqlite | none | medium | registered-inferred |
| `test_monitor_lease.py` | tests/ 根 | monitor | temporary_sqlite | none | medium | registered-inferred |
| `test_outbox.py` | tests/ 根 | outbox | temporary_sqlite | mocked | medium | registered-inferred |
| `test_outbox_429.py` | tests/ 根 | outbox | temporary_sqlite | mocked | medium | registered-inferred |
| `test_outbox_backoff.py` | tests/ 根 | outbox | temporary_sqlite | none | medium | registered-inferred |
| `test_outbox_http.py` | tests/ 根 | outbox | temporary_sqlite | mocked | medium | registered-inferred |
| `test_reminder_identity.py` | tests/ 根 | reminder | temporary_sqlite | none | medium | registered-inferred |
| `test_task_health.py` | tests/ 根 | task | temporary_sqlite | none | medium | registered-inferred |
| `test_task_run_history.py` | tests/ 根 | task | temporary_sqlite | none | medium | registered-inferred |

## api — L3 HTTP/API 测试 (11 files)

| File | 物理位置 | Domain | DB | Network | Risk | Status |
|---|---|---|---|---|---|---|
| `test_auth_api.py` | tests/ 根 | auth | temporary_sqlite | mocked | high | registered-inferred |
| `test_chat_budget_http.py` | tests/ 根 | billing | temporary_sqlite | mocked | high | registered-inferred |
| `test_chat_quota_http.py` | tests/ 根 | chat | temporary_sqlite | mocked | medium | registered-inferred |
| `test_chat_reconciliation_http.py` | tests/ 根 | billing | temporary_sqlite | mocked | high | registered-inferred |
| `test_chat_request_validation_http.py` | tests/ 根 | chat | temporary_sqlite | mocked | medium | registered-inferred |
| `test_chat_secure_api.py` | tests/ 根 | chat | temporary_sqlite | mocked | high | registered-inferred |
| `test_chat_usage_http.py` | tests/ 根 | chat | temporary_sqlite | mocked | medium | registered-inferred |
| `test_login_rate_limit.py` | tests/ 根 | auth | temporary_sqlite | mocked | high | registered-inferred |
| `test_monitor_api.py` | tests/ 根 | monitor | temporary_sqlite | none | high | registered-inferred |
| `test_reminder_api.py` | tests/ 根 | reminder | temporary_sqlite | none | high | registered |
| `test_reminder_chat_http.py` | tests/ 根 | reminder | temporary_sqlite | mocked | high | registered |

## legacy — 未分类测试 (0 files)

（无）

