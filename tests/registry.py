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

    # ----------------------------------------------------------------
    # L2：数据集成测试（临时 SQLite）
    # ----------------------------------------------------------------

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

    # L3：Bot 管理接口（创建 / 列表 / 身份卡 / 禁用，模拟身份）
    "test_bot_api.py": TestSchema(
        layer="api",
        domain="bot",
        database="temporary_sqlite",
        authentication="mocked",
        external_network="none",
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