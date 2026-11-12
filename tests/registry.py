"""Personal Agent Test Schema Registry.

测试元数据只描述测试依赖，不提供实际的环境隔离。

规则：
1. 每个测试文件只有一个主要层级。
2. 未登记的旧测试继续归入 legacy。
3. 现有测试不需要移动文件。

status 取值：
- registered          已登记（本次或此前登记，含静态证据）
- registered-inferred 本次补登记，元数据由静态源码阅读推断
- legacy              未登记，运行时按目录约定或 legacy 归类
- unknown             无法判断，按 legacy 处理

本次整理只补充元数据，不修改任何测试逻辑。
"""

from dataclasses import dataclass


LAYERS = frozenset({
    "unit",
    "integration",
    "api",
})

DATABASE_TYPES = frozenset({
    "none",
    "mocked",
    "temporary_sqlite",
    "real",
})

AUTH_TYPES = frozenset({
    "none",
    "mocked",
    "real",
})

NETWORK_TYPES = frozenset({
    "none",
    "mocked",
    "real",
})

RISK_LEVELS = frozenset({
    "low",
    "medium",
    "high",
})

# 登记状态。未出现在 TEST_SCHEMAS 中的文件按 UNREGISTERED_STATUS 处理。
REGISTERED = "registered"
REGISTERED_INFERRED = "registered-inferred"
LEGACY = "legacy"

DEFAULT_STATUS = REGISTERED
UNREGISTERED_STATUS = LEGACY


@dataclass(frozen=True)
class TestSchema:
    layer: str
    domain: str
    database: str
    authentication: str
    external_network: str
    risk: str

    def __post_init__(self):
        rules = {
            "layer": LAYERS,
            "database": DATABASE_TYPES,
            "authentication": AUTH_TYPES,
            "external_network": NETWORK_TYPES,
            "risk": RISK_LEVELS,
        }

        for field_name, allowed in rules.items():
            value = getattr(self, field_name)

            if value not in allowed:
                raise ValueError(
                    f"无效测试 Schema："
                    f"{field_name}={value!r}"
                )

        if (
            not isinstance(self.domain, str)
            or not self.domain.strip()
        ):
            raise ValueError(
                "测试 Schema 必须指定 domain"
            )


TEST_SCHEMAS = {

    # ----------------------------------------------------------------
    # L1：单元测试（无数据库，无真实网络）
    # ----------------------------------------------------------------

    # L1：提醒工具调用限制
    "unit/test_agent_tool_limits.py": TestSchema(
        layer="unit",
        domain="reminder",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L1：Python SDK 与语言中立 v1 fixture（内存传输）
    "unit/test_platform_sdk.py": TestSchema(
        layer="unit",
        domain="platform_sdk",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：Agent 工具调用次数上限
    "test_agent_call_count.py": TestSchema(
        layer="unit",
        domain="agent",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：Agent 输出长度上限与 provider 参数
    "test_agent_bounds.py": TestSchema(
        layer="unit",
        domain="agent",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：Agent 用量累计逻辑
    "test_agent_usage.py": TestSchema(
        layer="unit",
        domain="agent",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：RSS 监控错误传播（fetch_rss 被 patch）
    "test_rss_errors.py": TestSchema(
        layer="unit",
        domain="rss",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L1：订阅源 URL 安全策略（SSRF 防护，DNS 解析器注入）
    "unit/test_url_policy.py": TestSchema(
        layer="unit",
        domain="rss",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="high",
    ),

    # L1：提醒 eval 用例加载与判定（纯函数，不调用模型）
    "unit/test_reminder_eval.py": TestSchema(
        layer="unit",
        domain="eval",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：网页搜索 Provider（传输层注入，结果按不可信数据收敛）
    "unit/test_web_search.py": TestSchema(
        layer="unit",
        domain="web",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L1：正文抽取（静态 HTML 夹具，不联网）
    "unit/test_web_extract.py": TestSchema(
        layer="unit",
        domain="web",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：证据库（临时 SQLite、SHA256 去重、引用绑定）
    "unit/test_web_evidence.py": TestSchema(
        layer="unit",
        domain="web",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L1：研究侧模型调用层（completer 注入，不发请求）
    "unit/test_research_llm.py": TestSchema(
        layer="unit",
        domain="research",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L1：提示词 JSON 契约（纯校验函数）
    "unit/test_research_prompts.py": TestSchema(
        layer="unit",
        domain="research",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：ResearchGraph 状态机与两条回边（临时 SQLite 记账本）
    "unit/test_research_graph.py": TestSchema(
        layer="unit",
        domain="research",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L1：报告渲染与引用对账（临时 SQLite 证据库）
    "unit/test_research_report.py": TestSchema(
        layer="unit",
        domain="research",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # ----------------------------------------------------------------
    # L1：产物登记与路径安全（临时 SQLite，越界/穿越/符号链接）
    "unit/test_artifact_registry.py": TestSchema(
        layer="unit",
        domain="artifacts",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L1：中文活动摘要（纯函数聚合 span）
    "unit/test_activity_summary.py": TestSchema(
        layer="unit",
        domain="activity",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：代码修复闭环（固定 bug 夹具 + 受限子进程执行器）
    "unit/test_repair_loop.py": TestSchema(
        layer="unit",
        domain="repair",
        database="none",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L1：验收评分（机器代理 rubric + 硬门槛）
    "unit/test_scorecard.py": TestSchema(
        layer="unit",
        domain="eval",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：成本账本（失败运行也计入成本）
    "unit/test_cost_ledger.py": TestSchema(
        layer="unit",
        domain="cost",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：六类故障注入（传输层/事务/runner/sender 边界）
    "unit/test_fault_matrix.py": TestSchema(
        layer="unit",
        domain="faults",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="high",
    ),

    # L1：产物与 Outbox 对账
    "unit/test_fault_reconcile.py": TestSchema(
        layer="unit",
        domain="faults",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L1：视频探测（ffprobe 事实读取）
    "unit/test_video_probe.py": TestSchema(
        layer="unit",
        domain="video",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：语音转写（后端可注入）
    "unit/test_video_asr.py": TestSchema(
        layer="unit",
        domain="video",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：分段摘要（时间戳必须来自转写）
    "unit/test_video_summarize.py": TestSchema(
        layer="unit",
        domain="video",
        database="none",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L1：视频总结管线（三种无法验证路径）
    "unit/test_video_pipeline.py": TestSchema(
        layer="unit",
        domain="video",
        database="none",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L1：远程只读守卫（401/403/白名单/日志脱敏）
    "unit/test_remote_guard.py": TestSchema(
        layer="unit",
        domain="remote",
        database="none",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L1：访问日志与自签名证书
    "unit/test_remote_access_log.py": TestSchema(
        layer="unit",
        domain="remote",
        database="none",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：数据集成测试（临时 SQLite）
    # ----------------------------------------------------------------

    # L2：网页抓取安全边界（DNS 与会话注入，每一跳复检）
    "integration/test_web_fetch_safety.py": TestSchema(
        layer="integration",
        domain="web",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="high",
    ),

    # L2：search→fetch→extract→cite 闭环（抓取与抽取注入）
    "integration/test_web_pipeline.py": TestSchema(
        layer="integration",
        domain="web",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L1：记忆条目存储（同库新表、有效期、删除派生）
    "unit/test_auth_compat.py": TestSchema(
        layer="unit",
        domain="auth",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),



    "unit/test_memory_service.py": TestSchema(
        layer="unit",
        domain="memory",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    "unit/test_memory_tools.py": TestSchema(
        layer="unit",
        domain="memory",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="medium",
    ),

    "integration/test_memory_api.py": TestSchema(
        layer="integration",
        domain="memory",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="medium",
    ),

    "unit/test_review_bus.py": TestSchema(
        layer="unit",
        domain="collab",
        database="none",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    "unit/test_sessions.py": TestSchema(
        layer="unit",
        domain="knowledge",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    "unit/test_dsh_runner.py": TestSchema(
        layer="unit",
        domain="collab",
        database="none",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    "unit/test_orchestrator.py": TestSchema(
        layer="unit",
        domain="collab",
        database="none",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    "unit/test_memory_store.py": TestSchema(
        layer="unit",
        domain="memory",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),


    # L1：冲突解析（当前有效事实、信任等级、supersedes 链）
    "unit/test_memory_resolve.py": TestSchema(
        layer="unit",
        domain="memory",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L1：版本快照与回滚
    "unit/test_memory_versions.py": TestSchema(
        layer="unit",
        domain="memory",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L1：60 条记忆评测（免费指标 + 替身作答）
    "unit/test_memory_eval.py": TestSchema(
        layer="unit",
        domain="memory",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：研究端到端脚本（离线脚本化模型 + 固定证据，可复现）
    "integration/test_research_run.py": TestSchema(
        layer="integration",
        domain="research",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L2：终态唯一写（并发 finish 同一 exec 只允许一次成功）
    "integration/test_exec_terminal_write.py": TestSchema(
        layer="integration",
        domain="platform",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L2：记忆评测 CLI（免费档；付费档未确认即退出，不调模型）
    "integration/test_memory_eval_run.py": TestSchema(
        layer="integration",
        domain="memory",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L2：产物 API（鉴权替身 + 临时产物库，覆盖 4xx 安全用例）
    "integration/test_artifact_api.py": TestSchema(
        layer="integration",
        domain="artifacts",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L2：提醒 SQLite 存储与并发
    "integration/test_reminder_store.py": TestSchema(
        layer="integration",
        domain="reminder",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L2：任务引擎语义（基线 / 恰好一次 / 数据源注册表）
    "integration/test_task_pipeline.py": TestSchema(
        layer="integration",
        domain="monitor",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="high",
    ),

    # L2：运行时安全守卫（故意触发拦截，不产生真实网络流量）
    "integration/test_test_safety.py": TestSchema(
        layer="integration",
        domain="test_infra",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：安全守卫与 asyncio / socketpair 兼容性
    "integration/test_guarded_asyncio.py": TestSchema(
        layer="integration",
        domain="test_infra",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L2：tier1 runner 的工作区隔离 / 环境清洗 / 超时 / 输出上限 / 进程树清理
    # 真实创建 OS 子进程，因此 risk=high；数据库只用临时目录。
    "integration/test_tier1_runner.py": TestSchema(
        layer="integration",
        domain="platform_exec",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L2：助手 → 平台 exec → runner 的取消传播与 trajectory 事件顺序
    "integration/test_platform_exec_cancel.py": TestSchema(
        layer="integration",
        domain="platform_exec",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L2：D12 沙箱工具（read / write / ls / shell / python / http）与路径负向测试
    "integration/test_sandbox_tools.py": TestSchema(
        layer="integration",
        domain="platform_exec",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L2：D13 审批门（批准前无 exec、批准后恰好一次、拒绝 / 超时 / 取消 / 归属）
    "integration/test_approval_gate.py": TestSchema(
        layer="integration",
        domain="approval",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="medium",
    ),

    # L2：D14 轨迹渲染与 CLI（step / exec / 审批 / 事件）
    "integration/test_trajectory_cli.py": TestSchema(
        layer="integration",
        domain="trajectory",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L2：D15 幂等 create（同 key 重试 / 并发 / 指纹冲突 / 重启后仍生效）
    "integration/test_sandbox_create.py": TestSchema(
        layer="integration",
        domain="platform_create",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：D15 续 平台 v1 HTTP API（create / exec / get_exec / cancel / destroy）
    "integration/test_platform_api.py": TestSchema(
        layer="integration",
        domain="platform_http",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：D16 TTL 回收 / tombstone 保留 / 410 与惰性过期
    "integration/test_platform_ttl.py": TestSchema(
        layer="integration",
        domain="platform_ttl",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：D17 节点注册 / 心跳 / generation 与默认节点
    "integration/test_platform_nodes.py": TestSchema(
        layer="integration",
        domain="platform_nodes",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L2：D18 节点租约（3 个周期过期 / 续约 / 旧代际不能复活）
    "integration/test_platform_lease.py": TestSchema(
        layer="integration",
        domain="platform_nodes",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L2：D19 cordon / drain / remove（摘流不杀存量、drain 完才能下线）
    "integration/test_platform_node_ops.py": TestSchema(
        layer="integration",
        domain="platform_nodes",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：D20 沙箱状态机（running/idle/failed、非法迁移、旧库重建）
    "integration/test_sandbox_state.py": TestSchema(
        layer="integration",
        domain="platform_state",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：D21 每租户并发配额与节点容量（429 CAPACITY_EXCEEDED）
    "integration/test_sandbox_quota.py": TestSchema(
        layer="integration",
        domain="platform_quota",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：D22 启动对账（中断 exec、缺失工作区、孤儿目录）
    "integration/test_platform_reconcile.py": TestSchema(
        layer="integration",
        domain="platform_reconcile",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：D23 G2 全链路（一次响应 3 个工具、轨迹 3 组 exec）
    "integration/test_agent_multi_tool.py": TestSchema(
        layer="integration",
        domain="agent_tools",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="medium",
    ),

    # L2：D24 traceparent（宿主注入、子进程可见、一条 run 共用 trace-id）
    "integration/test_platform_trace.py": TestSchema(
        layer="integration",
        domain="platform_trace",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="medium",
    ),

    # L2：D25 /v1 Bearer 认证（缺/错 401、SDK 带头、未配置时的行为）
    "integration/test_platform_auth.py": TestSchema(
        layer="integration",
        domain="platform_auth",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="medium",
    ),

    # L2：D26 审计与限流（拒绝留痕、操作留痕、429）
    "integration/test_platform_audit.py": TestSchema(
        layer="integration",
        domain="platform_audit",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="medium",
    ),

    # L2：D30 有界队列（QUEUE_FULL、排队取消、名额释放）
    "integration/test_platform_queue.py": TestSchema(
        layer="integration",
        domain="platform_queue",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：D25' 指标（Prometheus 文本 + 六类指标）与 D26' JSONL 日志
    "integration/test_platform_metrics.py": TestSchema(
        layer="integration",
        domain="platform_metrics",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L2：D37 告警闭环（阈值 → 驱逐 → 审计）
    "integration/test_platform_alerts.py": TestSchema(
        layer="integration",
        domain="platform_alerts",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="low",
    ),
    # L2：最小 least-loaded 调度与单用户全局上限（修订版 D19/D21）
    "integration/test_platform_scheduling.py": TestSchema(
        layer="integration",
        domain="platform_nodes",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),
    # L2：D30 压测暴露的默认节点续租回归
    "integration/test_default_node_renewal.py": TestSchema(
        layer="integration",
        domain="platform_nodes",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L2：AI 预算预留、结算与并发
    "test_ai_budget.py": TestSchema(
        layer="integration",
        domain="billing",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L2：AI 实际用量与预算预留对账
    "test_ai_budget_reconcile.py": TestSchema(
        layer="integration",
        domain="billing",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L2：AI 计费单价与费用估算
    "test_ai_pricing.py": TestSchema(
        layer="integration",
        domain="billing",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L2：AI 用量落库与缓存 token 统计
    "test_ai_usage.py": TestSchema(
        layer="integration",
        domain="billing",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L2：AI 调用 trace 字段与旧库迁移
    "test_ai_usage_trace.py": TestSchema(
        layer="integration",
        domain="billing",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L2：agent 调用路径写入 trace（SDK 被 patch，临时用量库）
    "test_agent_usage_trace.py": TestSchema(
        layer="integration",
        domain="agent",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="high",
    ),

    # L2：route_decided 写入 trajectory
    "integration/test_route_events.py": TestSchema(
        layer="integration",
        domain="routing",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="low",
    ),
    # L1：确定性审批闸门
    "test_routing_policy.py": TestSchema(
        layer="unit",
        domain="routing",
        database="none",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：路由 → 沙箱 → 图表的垂直切片
    "integration/test_route_pipeline.py": TestSchema(
        layer="integration",
        domain="routing",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),
    # L1：路由评估集冻结与门槛
    "test_router_eval.py": TestSchema(
        layer="unit",
        domain="routing",
        database="none",
        authentication="none",
        external_network="none",
        risk="medium",
    ),
    # L1：弹性扩缩容（期望容量 + 冷却）
    "test_autoscale.py": TestSchema(
        layer="unit",
        domain="platform_nodes",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),
    # W3：扩缩容事件持久化与动作标签映射
    "test_scale_events.py": TestSchema(
        layer="unit",
        domain="platform_elastic",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="low",
    ),
    # W3：弹性控制器（真实容量口径、决策/事件分离、apply 模式）
    "test_elastic_controller.py": TestSchema(
        layer="unit",
        domain="platform_elastic",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="low",
    ),
    # W3：趋势采样（ring buffer、窗口、样本量）
    "test_trends.py": TestSchema(
        layer="unit",
        domain="platform_elastic",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="low",
    ),
    # W3：What-if 模拟（与真实决策同公式、只算不改）
    "test_whatif.py": TestSchema(
        layer="unit",
        domain="platform_elastic",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),
    # L1：降级不降风险
    "test_routing_fallback.py": TestSchema(
        layer="unit",
        domain="routing",
        database="none",
        authentication="none",
        external_network="none",
        risk="medium",
    ),
    # L2：控制台总览接口
    "integration/test_console_api.py": TestSchema(
        layer="integration",
        domain="platform_http",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="low",
    ),
    # W3：控制台快照（区块 envelope / 弹性口径 / 窗口指标）
    "integration/test_console_snapshot.py": TestSchema(
        layer="integration",
        domain="platform_http",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="low",
    ),
    # W3：控制台写操作（owner 鉴权、闭环、幂等、审计）
    "integration/test_console_actions.py": TestSchema(
        layer="integration",
        domain="platform_http",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="medium",
    ),
    # W3：控制台 What-if（默认取真实状态、模拟无副作用）
    "integration/test_console_simulate.py": TestSchema(
        layer="integration",
        domain="platform_http",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="low",
    ),
    # W3：控制台场景回放（只读持久事件、来源隔离）
    "integration/test_console_replay.py": TestSchema(
        layer="integration",
        domain="platform_http",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="low",
    ),
    # W3：控制台并发竞态（重复取消 / 重复销毁 / 终态再取消）
    "integration/test_console_concurrency.py": TestSchema(
        layer="integration",
        domain="platform_http",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="medium",
    ),
    # L2：单用户产品面（无注册/邀请入口）
    "integration/test_single_user_surface.py": TestSchema(
        layer="integration",
        domain="auth",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="low",
    ),
    # L2：备份 → 破坏 → 恢复 演练
    "integration/test_backup_restore.py": TestSchema(
        layer="integration",
        domain="backup",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L2：CLI 控制台离线只读
    "integration/test_console_cli.py": TestSchema(
        layer="integration",
        domain="console",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：监听地址解析（回环判定）
    "test_netcheck.py": TestSchema(
        layer="unit",
        domain="platform_ops",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),
    # L1：意图路由（规则层，不选节点）
    "test_intent_router.py": TestSchema(
        layer="unit",
        domain="routing",
        database="none",
        authentication="none",
        external_network="none",
        risk="medium",
    ),
    # L2：账号存储（建表、口令哈希、会话）
    "test_auth_store.py": TestSchema(
        layer="integration",
        domain="auth",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L2：首个 owner 引导与校验
    "test_bootstrap_owner.py": TestSchema(
        layer="integration",
        domain="auth",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L2：首次运行 HTTP 建站长（空库才允许，建完直接登录）
    "integration/test_auth_bootstrap.py": TestSchema(
        layer="integration",
        domain="auth",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L2：管理员告警落库
    "test_admin_alerts.py": TestSchema(
        layer="integration",
        domain="alerting",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：访问策略分发（requests 仅构造本地异常对象）
    "test_access_policy_dispatcher.py": TestSchema(
        layer="integration",
        domain="access_policy",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L2：outbox 重试（通知发送被 patch）
    "test_outbox.py": TestSchema(
        layer="integration",
        domain="outbox",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L2：outbox 429 限流退避
    "test_outbox_429.py": TestSchema(
        layer="integration",
        domain="outbox",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L2：outbox 指数退避
    "test_outbox_backoff.py": TestSchema(
        layer="integration",
        domain="outbox",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：outbox 发送成功路径（send_notification 被 patch）
    "test_outbox_http.py": TestSchema(
        layer="integration",
        domain="outbox",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L2：任务健康检查与命令队列
    "test_task_health.py": TestSchema(
        layer="integration",
        domain="task",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：任务运行历史记录
    "test_task_run_history.py": TestSchema(
        layer="integration",
        domain="task",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：调度器运行历史（路径由 tests.helper.isolate_paths 指向临时库）
    "test_dispatcher_history.py": TestSchema(
        layer="integration",
        domain="dispatcher",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：监控租约抢占用与释放
    #
    # 2026-10-14 迁移到 src.config.paths 后登记：
    # setUp 用 tests.helper.isolate_paths 同时替换 src.config.paths
    # 与各模块早绑定的 paths，受保护运行器下不再访问项目 tasks.db。
    "test_monitor_lease.py": TestSchema(
        layer="integration",
        domain="monitor",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：聊天准入判定
    "test_chat_admission.py": TestSchema(
        layer="integration",
        domain="chat",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：聊天请求归属校验
    "test_chat_ownership.py": TestSchema(
        layer="integration",
        domain="chat",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：聊天配额扣减与并发
    "test_chat_quota.py": TestSchema(
        layer="integration",
        domain="chat",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：聊天库重启恢复（init_chat_db，路径由 isolate_paths 指向临时目录）
    # 旧入口 src.api:app 下线后替代 test_api_idempotency.py 的重启用例。
    "test_chat_db_restart.py": TestSchema(
        layer="integration",
        domain="chat",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：提醒身份归属（REMINDER_DB_PATH 由 isolate_paths 指向临时目录）
    "test_reminder_identity.py": TestSchema(
        layer="integration",
        domain="reminder",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # ----------------------------------------------------------------
    # L3：HTTP 层测试（TestClient 进程内 ASGI，非真实网络）
    # ----------------------------------------------------------------

    # L3：个人站内提醒查询 API
    "test_reminder_api.py": TestSchema(
        layer="api",
        domain="reminder",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L3：监控任务 API（归属隔离 + 角色限制）
    "test_monitor_api.py": TestSchema(
        layer="api",
        domain="monitor",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L3：站内通知 API（按人投递的读取与确认）
    "test_notifications_api.py": TestSchema(
        layer="api",
        domain="monitor",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L3：统一收件箱（聚合监控通知与到点提醒）
    "test_inbox_api.py": TestSchema(
        layer="api",
        domain="monitor",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L2：提醒投递（调度器取件、幂等、不越权外推）
    "test_reminder_scheduler.py": TestSchema(
        layer="integration",
        domain="reminder",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="high",
    ),

    # L2：知识库（摄取幂等 / 归属隔离 / BM25 与短查询兜底）
    "test_knowledge.py": TestSchema(
        layer="integration",
        domain="knowledge",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L2：检索注入与引用（预算截断 / 空检索不注入 / [n] 可回溯）
    "test_knowledge_inject.py": TestSchema(
        layer="integration",
        domain="knowledge",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="high",
    ),

    # L2：向量与融合（确定性向量 / 内容缓存 / owner 隔离 / RRF 名次融合）
    "test_knowledge_vector.py": TestSchema(
        layer="integration",
        domain="knowledge",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L2：知识库软过期与归档（过期后检索不到 / 同 source_ref 归档旧版本）
    "test_knowledge_r5.py": TestSchema(
        layer="integration",
        domain="knowledge",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L2：Bot 实体（身份卡 / 双来源 / 单用户上限 3 / 归属隔离）
    "test_bot_store.py": TestSchema(
        layer="integration",
        domain="bot",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：流式模型、工具执行与取消留痕（SDK 全部模拟）
    "test_agent_stream.py": TestSchema(
        layer="integration",
        domain="agent_stream",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="mocked",
        risk="high",
    ),

    # L1：工具注册表与 allow / ask / deny 策略矩阵
    "test_tool_registry.py": TestSchema(
        layer="unit",
        domain="tool_policy",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L2：run / turn / step / exec 关联与归属隔离
    "test_run_store.py": TestSchema(
        layer="integration",
        domain="run_model",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L2：旧库副本的备份、原子迁移、幂等与回滚
    "test_run_migration.py": TestSchema(
        layer="integration",
        domain="run_model",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L3：Bot 管理接口（创建 / 列表 / 身份卡 / 禁用，模拟身份）
    "test_bot_api.py": TestSchema(
        layer="api",
        domain="bot",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="medium",
    ),

    # L2：单人聊天身份卡接入 + Bot 可见性（system prompt 纯函数 + SQLite）
    "test_bot_chat.py": TestSchema(
        layer="integration",
        domain="bot",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：Bot 独立记忆隔离（个人记忆 / bot 记忆按 bot_id 隔离）
    "test_bot_memory.py": TestSchema(
        layer="integration",
        domain="knowledge",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L3：独立 TTS 与正式应用路由（合成和网络均 mock）
    "test_tts_api.py": TestSchema(
        layer="api",
        domain="bot",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L3：经过认证的聊天提醒 HTTP 测试
    "test_reminder_chat_http.py": TestSchema(
        layer="api",
        domain="reminder",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="mocked",
        risk="high",
    ),

    # L3：登录与会话 API
    "test_auth_api.py": TestSchema(
        layer="api",
        domain="auth",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="mocked",
        risk="high",
    ),

    # L3：登录失败限流
    "test_login_rate_limit.py": TestSchema(
        layer="api",
        domain="auth",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="mocked",
        risk="high",
    ),

    # L3：聊天预算 HTTP 接口
    "test_chat_budget_http.py": TestSchema(
        layer="api",
        domain="billing",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="mocked",
        risk="high",
    ),

    # L3：聊天配额 HTTP 接口
    "test_chat_quota_http.py": TestSchema(
        layer="api",
        domain="chat",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="mocked",
        risk="medium",
    ),

    # L3：聊天用量 HTTP 接口
    "test_chat_usage_http.py": TestSchema(
        layer="api",
        domain="chat",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="mocked",
        risk="medium",
    ),

    # L3：聊天预算对账 HTTP 接口
    "test_chat_reconciliation_http.py": TestSchema(
        layer="api",
        domain="billing",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="mocked",
        risk="high",
    ),

    # L3：认证聊天安全接口
    "test_chat_secure_api.py": TestSchema(
        layer="api",
        domain="chat",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="mocked",
        risk="high",
    ),

    # L3：正式聊天 API 的 request_id 校验（模型被 patch）
    # 旧入口 src.api:app 下线后替代 test_api_http.py 的 400 用例。
    "test_chat_request_validation_http.py": TestSchema(
        layer="api",
        domain="chat",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="mocked",
        risk="medium",
    ),
    # L1：活动时间线（spans/execs/artifacts 三源合并）
    "unit/test_activity_timeline.py": TestSchema(
        layer="unit",
        domain="activity",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：数据生命周期（默认预演，确认才删）
    "unit/test_lifecycle_retention.py": TestSchema(
        layer="unit",
        domain="lifecycle",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L2：活动时间线 API（挂真实 router，鉴权用 override）
    "integration/test_activity_timeline_api.py": TestSchema(
        layer="integration",
        domain="activity",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="medium",
    ),
    # L1：外部 Agent 能力快照（unknown 即拒绝）
    "unit/test_collab_capability.py": TestSchema(
        layer="unit",
        domain="collab",
        database="none",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L1：任务注册表 / 工作区守卫 / 独立验收
    "unit/test_collab_registry.py": TestSchema(
        layer="unit",
        domain="collab",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),
    # L1：产物 Diff（文本 unified / 二进制摘要）
    "unit/test_diff_compare.py": TestSchema(
        layer="unit",
        domain="diff",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L2：Diff API（越权/缺失/不泄露绝对路径）
    "integration/test_diff_api.py": TestSchema(
        layer="integration",
        domain="diff",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="medium",
    ),

    # L1：运维 CLI（时间线 / 生命周期预演与执行）
    "unit/test_ops_cli.py": TestSchema(
        layer="unit",
        domain="ops",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L1：远程失败限速（阈值/冷却/清零/隔离）
    "unit/test_remote_ratelimit.py": TestSchema(
        layer="unit",
        domain="remote",
        database="none",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：真实入口路由（直接打 src.main.app，覆盖 /api 命名空间）
    "integration/test_main_app_routes.py": TestSchema(
        layer="integration",
        domain="api",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L1：远程令牌 → 只读 owner 会话桥接
    "unit/test_remote_bridge.py": TestSchema(
        layer="unit",
        domain="remote",
        database="none",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L1：外部任务 CLI（登记/列表/查看）
    "unit/test_collab_cli.py": TestSchema(
        layer="unit",
        domain="collab",
        database="temporary_sqlite",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：视频总结 API（鉴权/路径/时长/422 语义）
    "integration/test_video_api.py": TestSchema(
        layer="integration",
        domain="video",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L1：文件生命周期（报告/日志/关键帧，预演+确认）
    "unit/test_lifecycle_files.py": TestSchema(
        layer="unit",
        domain="lifecycle",
        database="none",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L1：ComfyUI 图生图工具（注册/参数/错误/超时）
    "unit/test_comfy_tools.py": TestSchema(
        layer="unit",
        domain="tools",
        database="none",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L2：ComfyUI 图生图端点（401/400/503/502/200）
    "integration/test_tools_api.py": TestSchema(
        layer="integration",
        domain="tools",
        database="none",
        authentication="mocked",
        external_network="none",
        risk="high",
    ),

    # L1：单次出图 CLI（steps 护栏/参数透传/退出码）
    "unit/test_comfy_once.py": TestSchema(
        layer="unit",
        domain="tools",
        database="none",
        authentication="none",
        external_network="none",
        risk="medium",
    ),

    # L1：GPU 保护自检（四条约束漂移检测）
    "unit/test_gpu_guard.py": TestSchema(
        layer="unit",
        domain="tools",
        database="none",
        authentication="none",
        external_network="none",
        risk="high",
    ),

    # L1：模拟 Provider（本地 HTTP，回环）
    "unit/test_mock_provider.py": TestSchema(
        layer="unit",
        domain="collab",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="low",
    ),

    # L1：文档一致性（README 计数 / GPU 常量漂移）
    "unit/test_docs_consistency.py": TestSchema(
        layer="unit",
        domain="docs",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：知识分块不变量（不丢内容 / 块长上限 / 结构边界）
    "unit/test_chunker.py": TestSchema(
        layer="unit",
        domain="knowledge",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：真实 Provider 适配器（OpenAI 兼容端点，HTTP 打桩）
    "unit/test_real_provider.py": TestSchema(
        layer="unit",
        domain="collab",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="mocked",
        risk="medium",
    ),

    # L1：external_task 工具（参数/路径安全/错误，全 mock）
    "unit/test_provider_tools.py": TestSchema(
        layer="unit",
        domain="agent_tools",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L1：Provider 工厂（ARK / OpenAI 兼容切换）
    "unit/test_provider_factory.py": TestSchema(
        layer="unit",
        domain="collab",
        database="none",
        authentication="none",
        external_network="none",
        risk="low",
    ),

    # L1：Qwen Code 外部 Agent Provider（子进程全 mock）
    "unit/test_qwen_agent.py": TestSchema(
        layer="unit",
        domain="collab",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L1：ntfy 通知出站（HTTP 全 mock）
    "unit/test_notification.py": TestSchema(
        layer="unit",
        domain="notification",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="low",
    ),

    # L1：QQ 入站（OneBot 签名/白名单/幂等/限流，全 mock）
    "unit/test_qq_inbound.py": TestSchema(
        layer="unit",
        domain="qq",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L1：QQ 入站端点固定 204（防 OneBot 快速回复双通道）
    "unit/test_qq_api.py": TestSchema(
        layer="unit",
        domain="qq",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L1：QQ 出站队列与 worker（SQLite 临时库，发送全 mock）
    "unit/test_qq_outbox.py": TestSchema(
        layer="unit",
        domain="qq",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L1：QQ 意图路由（test_qq_router.py）
    "unit/test_qq_router.py": TestSchema(
        layer="unit",
        domain="qq",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="low",
    ),
    # L1：QQ 意图路由（test_qq_policy.py）
    "unit/test_qq_policy.py": TestSchema(
        layer="unit",
        domain="qq",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),
    # L1：QQ 意图路由（test_qq_worker.py）
    "unit/test_qq_worker.py": TestSchema(
        layer="unit",
        domain="qq",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # L1：Provider 接力（test_relay.py）
    "unit/test_relay.py": TestSchema(
        layer="unit",
        domain="collab",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),
    # L1：Provider 接力（test_relay_store.py）
    "unit/test_relay_store.py": TestSchema(
        layer="unit",
        domain="collab",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),
    # L1：Provider 接力（test_relay_artifacts.py）
    "unit/test_relay_artifacts.py": TestSchema(
        layer="unit",
        domain="collab",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="low",
    ),

    # L1：Provider 接力 CLI（test_relay_cli.py）
    "unit/test_relay_cli.py": TestSchema(
        layer="unit",
        domain="collab",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # v1.4o（test_qq_threads.py）
    "unit/test_qq_threads.py": TestSchema(
        layer="unit",
        domain="qq",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="low",
    ),
    # v1.4o（test_external_task_failover.py）
    "unit/test_external_task_failover.py": TestSchema(
        layer="unit",
        domain="qq",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # v1.4a-2 Qwen Runner（test_qwen_events.py）
    "unit/test_qwen_events.py": TestSchema(
        layer="unit",
        domain="collab",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),
    # v1.4a-2 Qwen Runner（test_qwen_workspace.py）
    "unit/test_qwen_workspace.py": TestSchema(
        layer="unit",
        domain="collab",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),
    # v1.4a-2 Qwen Runner（test_qwen_runner.py）
    "unit/test_qwen_runner.py": TestSchema(
        layer="unit",
        domain="collab",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # v1.4a-3 Qwen CLI（test_qwen_cli.py）
    "unit/test_qwen_cli.py": TestSchema(
        layer="unit",
        domain="collab",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # v1.4a-3 Qwen Registry/Verify/通知（test_qwen_pipeline.py）
    "unit/test_qwen_pipeline.py": TestSchema(
        layer="unit",
        domain="collab",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # v1.4a-4 Qwen 目录监听（test_qwen_watcher.py）
    "unit/test_qwen_watcher.py": TestSchema(
        layer="unit",
        domain="collab",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # v1.4b QQ 同任务继续（test_qq_continue.py）
    "unit/test_qq_continue.py": TestSchema(
        layer="unit",
        domain="qq",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # v1.4c 跨 Agent 工件接力（test_handoff.py）
    "unit/test_handoff.py": TestSchema(
        layer="unit",
        domain="collab",
        database="temporary_sqlite",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # v1.3 聊天模型 fallback（test_agent_fallback.py）
    "test_agent_fallback.py": TestSchema(
        layer="unit",
        domain="agent",
        database="none",
        authentication="none",
        external_network="mocked",
        risk="medium",
    ),

    # v1.3 QQ 通知（test_qq_notify.py）
    "test_qq_notify.py": TestSchema(
        layer="unit",
        domain="notifications",
        database="none",
        authentication="mocked",
        external_network="mocked",
        risk="medium",
    ),

    # v1.3 QQ 通知（test_outbox_qq.py）
    "test_outbox_qq.py": TestSchema(
        layer="unit",
        domain="notifications",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="mocked",
        risk="medium",
    ),

    # v1.3 Office 转换（test_office_convert.py）
    "test_office_convert.py": TestSchema(
        layer="unit",
        domain="office",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="mocked",
        risk="high",
    ),
}


LAYER_ORDER = (
    "unit",
    "integration",
    "api",
)


def schema_status(filename: str) -> str:
    """Return the registration status of one test file."""

    if filename in TEST_SCHEMAS:
        return DEFAULT_STATUS

    return UNREGISTERED_STATUS


def expected_status(filename: str) -> str:
    """Status written by this整理 for a registered file.

    Registered entries created during the测试工程化整理 are
    inferred from static source review.
    """

    if filename not in TEST_SCHEMAS:
        return UNREGISTERED_STATUS

    if filename in ORIGINAL_SCHEMAS:
        return REGISTERED

    return REGISTERED_INFERRED


# 本次整理之前已经登记的 Schema。
ORIGINAL_SCHEMAS = frozenset({
    "unit/test_agent_tool_limits.py",
    "integration/test_reminder_store.py",
    "test_ai_budget.py",
    "test_ai_budget_reconcile.py",
    "test_reminder_api.py",
    "test_reminder_chat_http.py",
})


# ----------------------------------------------------------------
# 显式层归属
# ----------------------------------------------------------------
#
# 分层契约 = TEST_SCHEMAS 里的 layer 字段。
#
# 本表把它按层展开成「层 -> 文件名集合」，用于：
#   1. 查询：`python scripts/run_tests.py list integration`
#   2. 索引：`tests/TEST_LAYERS.md`（与代码同步校验）
#   3. 运行：run_tests.py 的分层选择直接查本表，
#      而不是靠目录约定推断（目录只作为未登记文件的兜底）
#
# 重要说明：
#   物理目录不是契约。tests/ 根目录下平铺的测试同样属于某一层，
#   迁移文件位置不会改变层归属，只会改变文件所在路径。
#
# 单一事实来源仍是 TEST_SCHEMAS；本表由它派生，并由
# validate_membership() 强制一致性，任何漂移都会在 audit 时报错。

LAYER_MEMBERSHIP = {
    layer: frozenset(
        filename
        for filename, schema in TEST_SCHEMAS.items()
        if schema.layer == layer
    )
    for layer in LAYER_ORDER
}


def layer_of(filename: str):
    """Return the registered layer of a test file, if any."""

    for layer, members_ in LAYER_MEMBERSHIP.items():
        if filename in members_:
            return layer

    return None


def members(layer: str) -> list:
    """Return the sorted file list of one layer."""

    if layer not in LAYER_MEMBERSHIP:
        raise ValueError(f"未定义的测试层：{layer}")

    return sorted(LAYER_MEMBERSHIP[layer])


def validate_membership(files=None) -> dict:
    """Check layer membership against the files actually on disk.

    Returns {"violations": [...], "warnings": [...]}.

    violations
        已登记但磁盘上找不到的文件（注册表过期，会直接打断运行）
    warnings
        磁盘上存在、却既没有登记也没有落在 layer 目录里的文件
        （会静默落入 legacy，无人过问）
    """

    violations = []
    warnings = []

    if files is None:
        return {"violations": violations, "warnings": warnings}

    on_disk = {path.as_posix() for path in files}

    for filename in sorted(TEST_SCHEMAS):
        if filename not in on_disk:
            violations.append(
                f"{filename}: 已登记但磁盘上不存在"
            )

    for filename in sorted(on_disk):
        if layer_of(filename) is not None:
            continue

        top = filename.split("/")[0] if "/" in filename else ""

        if top in LAYER_ORDER:
            # 目录约定仍然生效，只是没有 Schema 元数据。
            warnings.append(
                f"{filename}: 位于 {top}/ 但未登记 Schema，"
                "元数据缺失（层归属靠目录兜底）"
            )
        else:
            warnings.append(
                f"{filename}: 未登记且不在 layer 目录，"
                "将归入 legacy"
            )

    return {"violations": violations, "warnings": warnings}



def membership_digest() -> str:
    """Stable digest of the layer membership, for document sync."""

    import hashlib

    payload = "\n".join(
        f"{layer}:{','.join(sorted(LAYER_MEMBERSHIP[layer]))}"
        for layer in LAYER_ORDER
    )

    return hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()[:16]



# ----------------------------------------------------------------
# 已知隔离缺口
# ----------------------------------------------------------------
#
# 这里登记「元数据已审计、但暂不能作为分层测试运行」的文件。
#
# 字段：
#   expected_layer / domain / database / authentication /
#   external_network / risk
#       静态审计得到的应然元数据
#   reason
#       未能登记的具体原因
#   evidence
#       可复核的证据位置
#
# 2026-10-12 状态：**空**。
# 此前唯一一条（test_monitor_lease.py）已修复并登记为 integration，
# 见上方 TEST_SCHEMAS 中的注释。

KNOWN_ISOLATION_GAPS = {}


def isolation_gaps() -> dict:
    """Return the known isolation gaps."""

    return dict(KNOWN_ISOLATION_GAPS)


def summarize() -> dict:
    """Return a small registry summary for reports and audits."""

    counts = {
        layer: 0
        for layer in LAYER_ORDER
    }

    registered = 0
    inferred = 0

    for filename, schema in TEST_SCHEMAS.items():
        counts[schema.layer] = counts.get(schema.layer, 0) + 1

        if filename in ORIGINAL_SCHEMAS:
            registered += 1
        else:
            inferred += 1

    return {
        "schemas": len(TEST_SCHEMAS),
        "registered": registered,
        "registered_inferred": inferred,
        "by_layer": counts,
    }
