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

<!-- membership-digest: 4d32e089cc1b7315 -->

## unit — L1 单元测试 (32 files)

| File | 物理位置 | Domain | DB | Network | Risk | Status |
|---|---|---|---|---|---|---|
| `test_agent_bounds.py` | tests/ 根 | agent | none | none | low | registered-inferred |
| `test_agent_call_count.py` | tests/ 根 | agent | none | none | low | registered-inferred |
| `test_agent_usage.py` | tests/ 根 | agent | none | none | low | registered-inferred |
| `test_autoscale.py` | tests/ 根 | platform_nodes | none | none | low | registered-inferred |
| `test_elastic_controller.py` | tests/ 根 | platform_elastic | temporary_sqlite | none | low | registered-inferred |
| `test_intent_router.py` | tests/ 根 | routing | none | none | medium | registered-inferred |
| `test_netcheck.py` | tests/ 根 | platform_ops | none | none | low | registered-inferred |
| `test_router_eval.py` | tests/ 根 | routing | none | none | medium | registered-inferred |
| `test_routing_fallback.py` | tests/ 根 | routing | none | none | medium | registered-inferred |
| `test_routing_policy.py` | tests/ 根 | routing | none | none | medium | registered-inferred |
| `test_rss_errors.py` | tests/ 根 | rss | none | mocked | medium | registered-inferred |
| `test_scale_events.py` | tests/ 根 | platform_elastic | temporary_sqlite | none | low | registered-inferred |
| `test_tool_registry.py` | tests/ 根 | tool_policy | none | none | low | registered-inferred |
| `test_trends.py` | tests/ 根 | platform_elastic | temporary_sqlite | none | low | registered-inferred |
| `test_whatif.py` | tests/ 根 | platform_elastic | none | none | low | registered-inferred |
| `unit/test_activity_summary.py` | tests/unit/ | activity | none | none | low | registered-inferred |
| `unit/test_agent_tool_limits.py` | tests/unit/ | reminder | none | mocked | medium | registered |
| `unit/test_artifact_registry.py` | tests/unit/ | artifacts | temporary_sqlite | none | high | registered-inferred |
| `unit/test_memory_eval.py` | tests/unit/ | memory | temporary_sqlite | none | medium | registered-inferred |
| `unit/test_memory_resolve.py` | tests/unit/ | memory | temporary_sqlite | none | medium | registered-inferred |
| `unit/test_memory_store.py` | tests/unit/ | memory | temporary_sqlite | none | medium | registered-inferred |
| `unit/test_memory_versions.py` | tests/unit/ | memory | temporary_sqlite | none | medium | registered-inferred |
| `unit/test_platform_sdk.py` | tests/unit/ | platform_sdk | none | none | low | registered-inferred |
| `unit/test_reminder_eval.py` | tests/unit/ | eval | none | none | low | registered-inferred |
| `unit/test_research_graph.py` | tests/unit/ | research | temporary_sqlite | mocked | medium | registered-inferred |
| `unit/test_research_llm.py` | tests/unit/ | research | none | mocked | medium | registered-inferred |
| `unit/test_research_prompts.py` | tests/unit/ | research | none | none | low | registered-inferred |
| `unit/test_research_report.py` | tests/unit/ | research | temporary_sqlite | none | medium | registered-inferred |
| `unit/test_url_policy.py` | tests/unit/ | rss | none | mocked | high | registered-inferred |
| `unit/test_web_evidence.py` | tests/unit/ | web | temporary_sqlite | none | medium | registered-inferred |
| `unit/test_web_extract.py` | tests/unit/ | web | none | none | low | registered-inferred |
| `unit/test_web_search.py` | tests/unit/ | web | none | mocked | medium | registered-inferred |

## integration — L2 数据集成测试 (79 files)

| File | 物理位置 | Domain | DB | Network | Risk | Status |
|---|---|---|---|---|---|---|
| `integration/test_agent_multi_tool.py` | tests/integration/ | agent_tools | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_approval_gate.py` | tests/integration/ | approval | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_artifact_api.py` | tests/integration/ | artifacts | temporary_sqlite | none | high | registered-inferred |
| `integration/test_auth_bootstrap.py` | tests/integration/ | auth | temporary_sqlite | none | high | registered-inferred |
| `integration/test_backup_restore.py` | tests/integration/ | backup | temporary_sqlite | none | high | registered-inferred |
| `integration/test_console_actions.py` | tests/integration/ | platform_http | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_console_api.py` | tests/integration/ | platform_http | temporary_sqlite | none | low | registered-inferred |
| `integration/test_console_cli.py` | tests/integration/ | console | temporary_sqlite | none | low | registered-inferred |
| `integration/test_console_concurrency.py` | tests/integration/ | platform_http | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_console_replay.py` | tests/integration/ | platform_http | temporary_sqlite | none | low | registered-inferred |
| `integration/test_console_simulate.py` | tests/integration/ | platform_http | temporary_sqlite | none | low | registered-inferred |
| `integration/test_console_snapshot.py` | tests/integration/ | platform_http | temporary_sqlite | none | low | registered-inferred |
| `integration/test_default_node_renewal.py` | tests/integration/ | platform_nodes | temporary_sqlite | none | low | registered-inferred |
| `integration/test_exec_terminal_write.py` | tests/integration/ | platform | temporary_sqlite | none | high | registered-inferred |
| `integration/test_guarded_asyncio.py` | tests/integration/ | test_infra | none | none | low | registered-inferred |
| `integration/test_memory_eval_run.py` | tests/integration/ | memory | temporary_sqlite | mocked | medium | registered-inferred |
| `integration/test_platform_alerts.py` | tests/integration/ | platform_alerts | temporary_sqlite | none | low | registered-inferred |
| `integration/test_platform_api.py` | tests/integration/ | platform_http | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_platform_audit.py` | tests/integration/ | platform_audit | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_platform_auth.py` | tests/integration/ | platform_auth | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_platform_exec_cancel.py` | tests/integration/ | platform_exec | temporary_sqlite | none | high | registered-inferred |
| `integration/test_platform_lease.py` | tests/integration/ | platform_nodes | temporary_sqlite | none | low | registered-inferred |
| `integration/test_platform_metrics.py` | tests/integration/ | platform_metrics | temporary_sqlite | none | low | registered-inferred |
| `integration/test_platform_node_ops.py` | tests/integration/ | platform_nodes | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_platform_nodes.py` | tests/integration/ | platform_nodes | temporary_sqlite | none | low | registered-inferred |
| `integration/test_platform_queue.py` | tests/integration/ | platform_queue | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_platform_reconcile.py` | tests/integration/ | platform_reconcile | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_platform_scheduling.py` | tests/integration/ | platform_nodes | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_platform_trace.py` | tests/integration/ | platform_trace | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_platform_ttl.py` | tests/integration/ | platform_ttl | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_reminder_store.py` | tests/integration/ | reminder | temporary_sqlite | none | high | registered |
| `integration/test_research_run.py` | tests/integration/ | research | temporary_sqlite | mocked | medium | registered-inferred |
| `integration/test_route_events.py` | tests/integration/ | routing | temporary_sqlite | none | low | registered-inferred |
| `integration/test_route_pipeline.py` | tests/integration/ | routing | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_sandbox_create.py` | tests/integration/ | platform_create | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_sandbox_quota.py` | tests/integration/ | platform_quota | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_sandbox_state.py` | tests/integration/ | platform_state | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_sandbox_tools.py` | tests/integration/ | platform_exec | temporary_sqlite | none | high | registered-inferred |
| `integration/test_single_user_surface.py` | tests/integration/ | auth | temporary_sqlite | none | low | registered-inferred |
| `integration/test_task_pipeline.py` | tests/integration/ | monitor | temporary_sqlite | mocked | high | registered-inferred |
| `integration/test_test_safety.py` | tests/integration/ | test_infra | temporary_sqlite | none | medium | registered-inferred |
| `integration/test_tier1_runner.py` | tests/integration/ | platform_exec | temporary_sqlite | none | high | registered-inferred |
| `integration/test_trajectory_cli.py` | tests/integration/ | trajectory | temporary_sqlite | none | low | registered-inferred |
| `integration/test_web_fetch_safety.py` | tests/integration/ | web | none | mocked | high | registered-inferred |
| `integration/test_web_pipeline.py` | tests/integration/ | web | temporary_sqlite | mocked | medium | registered-inferred |
| `test_access_policy_dispatcher.py` | tests/ 根 | access_policy | temporary_sqlite | mocked | medium | registered-inferred |
| `test_admin_alerts.py` | tests/ 根 | alerting | temporary_sqlite | none | medium | registered-inferred |
| `test_agent_stream.py` | tests/ 根 | agent_stream | temporary_sqlite | mocked | high | registered-inferred |
| `test_agent_usage_trace.py` | tests/ 根 | agent | temporary_sqlite | mocked | high | registered-inferred |
| `test_ai_budget.py` | tests/ 根 | billing | temporary_sqlite | none | high | registered |
| `test_ai_budget_reconcile.py` | tests/ 根 | billing | temporary_sqlite | none | high | registered |
| `test_ai_pricing.py` | tests/ 根 | billing | temporary_sqlite | none | high | registered-inferred |
| `test_ai_usage.py` | tests/ 根 | billing | temporary_sqlite | none | high | registered-inferred |
| `test_ai_usage_trace.py` | tests/ 根 | billing | temporary_sqlite | none | high | registered-inferred |
| `test_auth_store.py` | tests/ 根 | auth | temporary_sqlite | none | high | registered-inferred |
| `test_bootstrap_owner.py` | tests/ 根 | auth | temporary_sqlite | none | high | registered-inferred |
| `test_bot_chat.py` | tests/ 根 | bot | temporary_sqlite | none | medium | registered-inferred |
| `test_bot_memory.py` | tests/ 根 | knowledge | temporary_sqlite | none | high | registered-inferred |
| `test_bot_store.py` | tests/ 根 | bot | temporary_sqlite | none | medium | registered-inferred |
| `test_chat_admission.py` | tests/ 根 | chat | temporary_sqlite | none | medium | registered-inferred |
| `test_chat_db_restart.py` | tests/ 根 | chat | temporary_sqlite | none | medium | registered-inferred |
| `test_chat_ownership.py` | tests/ 根 | chat | temporary_sqlite | none | medium | registered-inferred |
| `test_chat_quota.py` | tests/ 根 | chat | temporary_sqlite | none | medium | registered-inferred |
| `test_dispatcher_history.py` | tests/ 根 | dispatcher | temporary_sqlite | none | medium | registered-inferred |
| `test_knowledge.py` | tests/ 根 | knowledge | temporary_sqlite | none | high | registered-inferred |
| `test_knowledge_inject.py` | tests/ 根 | knowledge | temporary_sqlite | mocked | high | registered-inferred |
| `test_knowledge_r5.py` | tests/ 根 | knowledge | temporary_sqlite | none | high | registered-inferred |
| `test_knowledge_vector.py` | tests/ 根 | knowledge | temporary_sqlite | none | high | registered-inferred |
| `test_monitor_lease.py` | tests/ 根 | monitor | temporary_sqlite | none | medium | registered-inferred |
| `test_outbox.py` | tests/ 根 | outbox | temporary_sqlite | mocked | medium | registered-inferred |
| `test_outbox_429.py` | tests/ 根 | outbox | temporary_sqlite | mocked | medium | registered-inferred |
| `test_outbox_backoff.py` | tests/ 根 | outbox | temporary_sqlite | none | medium | registered-inferred |
| `test_outbox_http.py` | tests/ 根 | outbox | temporary_sqlite | mocked | medium | registered-inferred |
| `test_reminder_identity.py` | tests/ 根 | reminder | temporary_sqlite | none | medium | registered-inferred |
| `test_reminder_scheduler.py` | tests/ 根 | reminder | temporary_sqlite | mocked | high | registered-inferred |
| `test_run_migration.py` | tests/ 根 | run_model | temporary_sqlite | none | high | registered-inferred |
| `test_run_store.py` | tests/ 根 | run_model | temporary_sqlite | none | high | registered-inferred |
| `test_task_health.py` | tests/ 根 | task | temporary_sqlite | none | medium | registered-inferred |
| `test_task_run_history.py` | tests/ 根 | task | temporary_sqlite | none | medium | registered-inferred |

## api — L3 HTTP/API 测试 (15 files)

| File | 物理位置 | Domain | DB | Network | Risk | Status |
|---|---|---|---|---|---|---|
| `test_auth_api.py` | tests/ 根 | auth | temporary_sqlite | mocked | high | registered-inferred |
| `test_bot_api.py` | tests/ 根 | bot | temporary_sqlite | none | medium | registered-inferred |
| `test_chat_budget_http.py` | tests/ 根 | billing | temporary_sqlite | mocked | high | registered-inferred |
| `test_chat_quota_http.py` | tests/ 根 | chat | temporary_sqlite | mocked | medium | registered-inferred |
| `test_chat_reconciliation_http.py` | tests/ 根 | billing | temporary_sqlite | mocked | high | registered-inferred |
| `test_chat_request_validation_http.py` | tests/ 根 | chat | temporary_sqlite | mocked | medium | registered-inferred |
| `test_chat_secure_api.py` | tests/ 根 | chat | temporary_sqlite | mocked | high | registered-inferred |
| `test_chat_usage_http.py` | tests/ 根 | chat | temporary_sqlite | mocked | medium | registered-inferred |
| `test_inbox_api.py` | tests/ 根 | monitor | temporary_sqlite | none | high | registered-inferred |
| `test_login_rate_limit.py` | tests/ 根 | auth | temporary_sqlite | mocked | high | registered-inferred |
| `test_monitor_api.py` | tests/ 根 | monitor | temporary_sqlite | none | high | registered-inferred |
| `test_notifications_api.py` | tests/ 根 | monitor | temporary_sqlite | none | high | registered-inferred |
| `test_reminder_api.py` | tests/ 根 | reminder | temporary_sqlite | none | high | registered |
| `test_reminder_chat_http.py` | tests/ 根 | reminder | temporary_sqlite | mocked | high | registered |
| `test_tts_api.py` | tests/ 根 | bot | temporary_sqlite | mocked | medium | registered-inferred |

## legacy — 未分类测试 (0 files)

（无）

