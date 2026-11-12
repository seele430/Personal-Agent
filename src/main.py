"""Personal Agent 正式后端入口。

负责：
- 初始化聊天数据库
- 初始化用户归属及 AI 预算表
- 注册认证、聊天和个人提醒路由
"""

from contextlib import asynccontextmanager, closing

import hashlib
import hmac
import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from src import api as legacy_chat
from src.auth_api import router as auth_router
from src.bot_api import router as bot_router
from src.chat_secure_api import router as chat_router
from src.console_api import router as console_router
from src.inbox_api import router as inbox_router
from src.monitors_api import router as monitor_router
from src.notifications_api import router as notification_router
from src.platform_api import router as platform_router
from src.platform.audit import AuditLog, audit_retention_days
from src.platform.elastic_controller import (
    ensure_schema as ensure_scale_events_schema,
    start_controller as start_elastic_controller,
    stop_controller as stop_elastic_controller,
)
from src.platform.rate_limit import get_limiter
from src.platform.exec_queue import start_watchdog, stop_watchdog
from src.platform.reaper import start_reaper, stop_reaper
from src.platform.reconcile import reconcile_startup
from src.platform.trends import start_sampler, stop_sampler
from src.reminder_api import router as reminder_router
from src.tts_api import router as tts_router
from src.teams_api import router as teams_router
from src.qq.api import router as qq_router
from src.sessions_api import router as sessions_router
from src.activity_api import router as activity_router
from src.tools_api import router as tools_router
from src.video_api import router as video_router
from src.artifacts_api import router as artifacts_router
from src.office_api import router as office_router
from src.memory_api import router as memory_router

from src.usage_api import router as usage_router

logger = logging.getLogger("personal_agent.main")

from src.core.chat_ownership import migrate_chat_ownership
from src.core.ai_budget import init_budget_schema
from src.core.log_json import configure_json_logging, json_logging_enabled
from src.core.trace_context import bind_trace, new_trace_id, start_span


# ==========================================
# 应用启动及数据库初始化
# ==========================================

@asynccontextmanager
async def lifespan(_app: FastAPI):

    # 初始化原有聊天数据库。
    legacy_chat.init_chat_db()

    # 在同一个聊天数据库中，
    # 确保用户归属结构及预算表存在。
    with closing(legacy_chat.connect_db()) as conn:
        with conn:
            migrate_chat_ownership(conn)
            init_budget_schema(conn)

    # D22：启动对账 —— 上次进程留下的中断 exec / 孤儿沙箱先收敛，再开回收线程。
    try:
        reconcile_startup()
    except Exception:
        logger.exception("启动对账失败")

    # D25：没配令牌就明确说清楚，别让人以为 /v1 已经受保护。
    # 但「设了却全是空白」不算没配 —— 那等于静默关掉认证，必须拒绝启动。
    raw_token = os.getenv(PLATFORM_TOKEN_ENV, "")

    if raw_token and not raw_token.strip():
        raise RuntimeError(
            "PERSONAL_AGENT_PLATFORM_TOKEN 已设置但内容全是空白；"
            "要么填真实令牌，要么删除该变量"
        )

    if platform_token() is None:
        if allow_anon_v1():
            logger.warning(
                "%s=1：/v1 写操作允许匿名访问，仅限本机开发",
                ALLOW_ANON_ENV,
            )
        else:
            logger.warning(
                "PERSONAL_AGENT_PLATFORM_TOKEN 未配置：/v1 读可用，"
                "写操作默认拒绝（本机开发可设 %s=1）",
                ALLOW_ANON_ENV,
            )

    # D26.1：启动时按保留期清理旧审计。
    try:
        pruned = AuditLog().prune(audit_retention_days())

        if pruned:
            logger.info("审计清理：删除 %s 条过期记录", pruned)
    except Exception:
        logger.exception("审计清理失败")

    # D26'：结构化 JSONL 日志（默认关闭，`PERSONAL_AGENT_LOG_JSON=1` 打开）。
    if json_logging_enabled():
        configure_json_logging()
        logger.info("JSONL 日志已启用")

    # D16：平台 TTL 回收线程；测试环境用 PERSONAL_AGENT_PLATFORM_REAPER=0 关闭。
    start_reaper()

    # D30.2：排队超时看门狗（等太久的 queued exec 落 timed_out）。
    start_watchdog()

    # W3：弹性事件表（读接口不背建表副作用）；控制器默认关闭，
    # 只有 PERSONAL_AGENT_ELASTIC=1 时才起后台 tick。
    try:
        ensure_scale_events_schema()
    except Exception:
        logger.exception("弹性事件表初始化失败")

    try:
        start_elastic_controller()
    except Exception:
        logger.exception("弹性控制器启动失败")

    # W3：趋势采样（进程内 ring buffer，重启即丢；PERSONAL_AGENT_TRENDS=0 关闭）。
    try:
        start_sampler()
    except Exception:
        logger.exception("趋势采样启动失败")

    try:
        yield
    finally:
        stop_sampler()
        stop_elastic_controller()
        stop_watchdog()
        stop_reaper()


# ==========================================
# FastAPI 应用
# ==========================================

app = FastAPI(
    title="Personal Agent API",
    lifespan=lifespan,
)


# ==========================================
# 请求级 trace：每个请求一条链路
# ==========================================
#
# trace_id 会回写到响应头，前端/脚本可以直接拿。
# 静态资源不记，否则噪音太大。

_TRACE_SKIP_PREFIXES = ("/assets/", "/favicon")


def _valid_trace_id(value: str) -> bool:
    if len(value) != 32:
        return False

    return all(
        char in "0123456789abcdef"
        for char in value.lower()
    )


@app.middleware("http")
async def _trace_request(request, call_next):
    path = request.url.path

    if (
        path.startswith(_TRACE_SKIP_PREFIXES)
        or request.method == "OPTIONS"
    ):
        return await call_next(request)

    incoming = request.headers.get("x-trace-id", "").strip()

    trace_id = (
        incoming.lower()
        if _valid_trace_id(incoming)
        else new_trace_id()
    )

    with bind_trace(trace_id):
        handle = start_span(
            f"{request.method} {path}",
            path=path,
        )

        try:
            response = await call_next(request)
        except Exception:
            handle.end("error")
            raise

        handle.end(
            "ok" if response.status_code < 500 else "error"
        )

        response.headers["X-Trace-Id"] = trace_id

        return response


PLATFORM_TOKEN_ENV = "PERSONAL_AGENT_PLATFORM_TOKEN"

# 未配置令牌时：/v1 的写请求默认拒绝；显式设 1 才恢复“匿名可写”的旧行为。
ALLOW_ANON_ENV = "PERSONAL_AGENT_ALLOW_ANON_V1"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 审计查询前缀：已认证方不占限流额度（事故排查用）。
_AUDIT_PATH_PREFIX = "/v1/audit"


def allow_anon_v1() -> bool:
    return os.getenv(ALLOW_ANON_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def platform_token() -> str | None:
    """平台访问令牌；未配置返回 None（此时 /v1 保持本机可用的既有行为）。"""

    return os.getenv(PLATFORM_TOKEN_ENV, "").strip() or None


def _bearer_of(request) -> str | None:
    """取 Bearer 值；缺头 / 非 Bearer / 空值一律 None。

    全进程只在这里解析 Authorization，认证中间件复用它的结论，避免
    「键函数」和「认证」各自解析后对同一请求给出不同判断。
    """

    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")

    if scheme.lower() != "bearer":
        return None

    value = value.strip()

    return value or None


def _actor_key_and_auth(request) -> tuple[str, bool]:
    """返回 (限流/审计键, 是否已认证)。

    键描述的是「谁」，不是「服务端配了什么」：已认证方按出示令牌的指纹
    分桶，未认证方按来源主机分桶。否则匿名流量会和合法客户端共用同一个
    桶，把对方额度打空。
    """

    token = platform_token()
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) or "unknown"

    if token is None:
        return f"host:{host}", True

    provided = _bearer_of(request)

    # 用 UTF-8 字节比较：非 ASCII 的 str 直接进 compare_digest 会 TypeError。
    valid = provided is not None and hmac.compare_digest(
        provided.encode("utf-8"),
        token.encode("utf-8"),
    )

    if valid:
        return (
            "tok:" + hashlib.sha256(provided.encode("utf-8")).hexdigest()[:12],
            True,
        )

    return f"anon:{host}", False


def _platform_actor_key(request) -> str:
    """限流与审计的键；认证结论由 `_actor_key_and_auth` 提供。"""

    return _actor_key_and_auth(request)[0]



def _audit_denied(request, event: str, code: str, *, auth_ok: bool) -> None:
    """拒绝路径的审计；写失败不影响响应。"""

    try:
        AuditLog().record(
            event,
            outcome="denied",
            code=code,
            actor=_platform_actor_key(request),
            detail={"path": request.url.path, "auth_ok": auth_ok},
        )
    except Exception:  # noqa: BLE001 - 审计是旁路
        logger.exception("审计写入失败 event=%s", event)


@app.middleware("http")
async def _platform_auth(request, call_next):
    """D25/D26：/v1 的进程内限流 + Bearer 认证。

    顺序是「限流 → 认证」而不是反过来：D25 的遗留是 401 可以无限重试，
    D26 有意让失败认证也付出限流成本。真正要修的是键 —— 它必须描述
    调用方，否则匿名流量会和合法客户端抢同一个桶。

    配了 `PERSONAL_AGENT_PLATFORM_TOKEN` 就强制校验；没配则放行并在启动时
    打警告 —— 未配置意味着「只监听本机」这条边界还在生效。
    `PERSONAL_AGENT_PLATFORM_RATE_PER_MIN=0` 显式关闭限流（测试与受控环境）。
    """

    if not request.url.path.startswith("/v1"):
        return await call_next(request)

    key, authed = _actor_key_and_auth(request)
    limiter = get_limiter()

    # 审计查询要能在事故中随取随用，不占限流额度；但只对已认证方开放，
    # 否则匿名请求可以无限打 /v1/audit，等于开了个免限流口子。
    audit_exempt = authed and request.url.path.startswith(_AUDIT_PATH_PREFIX)

    if (
        limiter is not None
        and not audit_exempt
        and not limiter.allow(key)
    ):
        _audit_denied(
            request,
            "rate_limited",
            "RATE_LIMITED",
            auth_ok=authed,
        )
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "code": "RATE_LIMITED",
                    "message": "请求过于频繁，请稍后重试",
                    "retryable": True,
                    "request_id": None,
                }
            },
            headers={
                "Retry-After": str(limiter.retry_after_seconds(key)),
            },
        )

    # R6：未配置令牌时，写请求默认拒绝；读请求沿用旧行为。
    # 注意 `authed` 在「未配令牌」模式下是 True（d26.2 的兼容语义），
    # 所以这里必须显式看 platform_token()，不能只看 authed。
    if (
        platform_token() is None
        and not allow_anon_v1()
        and request.method.upper() in WRITE_METHODS
    ):
        _audit_denied(
            request,
            "auth.denied",
            "UNAUTHORIZED",
            auth_ok=False,
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": "UNAUTHORIZED",
                    "message": (
                        "未配置平台令牌：/v1 写操作默认拒绝；"
                        f"本机开发需要时设置 {ALLOW_ANON_ENV}=1"
                    ),
                    "retryable": False,
                    "request_id": None,
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not authed:
        _audit_denied(
            request,
            "auth.denied",
            "UNAUTHORIZED",
            auth_ok=False,
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": "UNAUTHORIZED",
                    "message": "缺少或错误的平台访问令牌",
                    "retryable": False,
                    "request_id": None,
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    return await call_next(request)


@app.get("/health")
def health():
    return {
        "status": "ok",
    }


@app.get("/metrics")
def metrics():
    """D25'：Prometheus 文本格式指标。"""

    from fastapi.responses import Response

    from src.platform.metrics import TEXT_CONTENT_TYPE, render

    return Response(content=render(), media_type=TEXT_CONTENT_TYPE)


# ==========================================
# 注册正式接口
# ==========================================

# 登录、身份查询和退出登录
app.include_router(auth_router)

# Bot 管理（创建/列表/身份卡/禁用）
app.include_router(bot_router)

# Bot 试听与既有语音播放
app.include_router(tts_router)

# 已认证聊天、额度及费用保护
app.include_router(chat_router)

# 沙箱平台 v1（create / exec / get_exec / cancel / destroy）
app.include_router(platform_router)

# 个人站内提醒查询
app.include_router(reminder_router)

# 个人额度查询（只读）
app.include_router(usage_router)

# 监控任务（按归属隔离，全局调度）
app.include_router(monitor_router)

# 站内通知（非站长用户的投递渠道）
app.include_router(notification_router)

# 平台控制台（路由决策 / 节点 / 沙箱 / 执行一屏）
app.include_router(console_router)

# 统一收件箱（聚合监控通知与到点提醒）
app.include_router(inbox_router)

# 团队任务分配
app.include_router(teams_router)

# QQ 入站（OneBot 11，仅白名单私聊）
app.include_router(qq_router)
app.include_router(sessions_router)

# 产物登记、四类预览与安全下载（W4）
app.include_router(artifacts_router)
app.include_router(office_router)
app.include_router(memory_router)

# 中文活动摘要卡（W4）
app.include_router(activity_router)

# ComfyUI 图生图（v1.6，需登录；远程白名单不含 /api/tools）
app.include_router(tools_router)

# 视频总结（v1.1.11，受控同步接口）
app.include_router(video_router)


# 兼容前端历史路径：/api/auth/* → /auth/*
@app.middleware("http")
async def _compat_api_auth_prefix(request, call_next):
    path = request.url.path

    for prefix in (
        "/api/auth", "/api/chat", "/api/reminders", "/api/console",
        "/api/monitors", "/api/notifications", "/api/inbox",
        "/api/teams", "/api/usage", "/api/bots", "/api/sessions",
    ):
        if path == prefix or path.startswith(prefix + "/"):
            request.scope["path"] = path[len("/api"):]
            break

    return await call_next(request)


# ==========================================
# 桌面版：兼容 /api 前缀
# ==========================================
#
# 规范前缀就是 `/api`：所有 router、后端集成测试、Playwright mock、前端调用
# 都用 `/api/...`。这里**不再剥前缀**——2026-11-04 那版曾把 `/api/artifacts`
# 改写成 `/artifacts`，导致真实入口下所有接口 404（见 pa-v122-evidence 的
# BLOCKER-api-prefix.md）。现在只保留一条有用行为：`/api/*` 方法不匹配时
# 统一回 404，不要让 SPA 兜底把它变成页面或 405。
@app.middleware("http")
async def _api_method_not_allowed_to_404(request, call_next):
    is_api = request.url.path.startswith("/api/")

    response = await call_next(request)

    if is_api and response.status_code == 405:
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "Not Found"}, status_code=404)

    return response


# ==========================================
# 桌面版：由后端直接托管前端静态文件
# ==========================================
#
# 开发模式前后端分离（Vite 5173 + api 8000），打包成桌面应用后
# 只有一个进程，窗口直接打开 http://127.0.0.1:8000 —— 所以这里把
# frontend/dist 挂上去。找不到 dist 就静默跳过，不影响开发模式。
def _mount_frontend() -> None:
    import os
    import sys

    from fastapi.staticfiles import StaticFiles

    if getattr(sys, "frozen", False):
        # PyInstaller 解包目录（spec 里把 dist 放这里）
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )

    dist = os.path.join(base, "frontend_dist")

    if not os.path.isdir(dist):
        dist = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..",
            "frontend",
            "dist",
        )

    if os.path.isdir(dist):
        from fastapi.responses import FileResponse

        index = os.path.join(dist, "index.html")
        assets = os.path.join(dist, "assets")

        if os.path.isdir(assets):
            app.mount(
                "/assets",
                StaticFiles(directory=assets),
                name="assets",
            )

        # SPA 回退：/login、/chat 这类前端路由刷新时也必须拿到 index.html，
        # 否则直接 404（StaticFiles 只服务真实存在的文件）。
        # /api/* 是接口命名空间：剥离前缀后仍返回 404，不能回退到页面。
        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str, request: Request):
            if request.scope.get("_was_api_prefix"):
                raise HTTPException(status_code=404)

            if full_path.startswith(("api/", "auth/", "v1/", "assets/")):
                raise HTTPException(status_code=404)

            if not os.path.isfile(index):
                raise HTTPException(status_code=404)

            return FileResponse(index)


_mount_frontend()


# ==========================================
# 远程只读模式（v1.2）：只在显式开启时包装整个 app
# ==========================================
# 守卫不装载 = 远程开关形同虚设（2026-11-08 的 P0 之一）。
# 默认关闭：没设 PERSONAL_AGENT_REMOTE=1 与足够长的令牌时不包装。
def _install_remote_guard(application):
    from src.remote.config import from_env

    config = from_env()

    if not config.usable:
        return application

    from src.remote.guard import RemoteGuard

    wrapped = RemoteGuard(application, config)

    # owner 鉴权（v1.2.4）：只有显式配置了 owner 才安装会话桥接。
    # 没配就维持"令牌只过守卫、应用层仍要登录"，不默认成站长。
    if config.owner_id:
        from src.auth_api import bearer_scheme, get_auth_store, get_current_user
        from src.remote.bridge import install

        install(
            application,
            get_current_user,
            bearer_scheme=bearer_scheme,
            get_auth_store=get_auth_store,
        )

    return wrapped


app = _install_remote_guard(app)
