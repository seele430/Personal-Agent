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
    if platform_token() is None:
        logger.warning(
            "PERSONAL_AGENT_PLATFORM_TOKEN 未配置：/v1 无认证，只能在本机使用"
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


def platform_token() -> str | None:
    """平台访问令牌；未配置返回 None（此时 /v1 保持本机可用的既有行为）。"""

    return os.getenv(PLATFORM_TOKEN_ENV, "").strip() or None


def _platform_actor_key(request) -> str:
    """限流与审计的键：令牌指纹优先，其次来源主机。"""

    token = platform_token()

    if token:
        return "tok:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]

    host = request.client.host if request.client else "unknown"

    return f"host:{host}"


def _audit_denied(request, event: str, code: str) -> None:
    """拒绝路径的审计；写失败不影响响应。"""

    try:
        AuditLog().record(
            event,
            outcome="denied",
            code=code,
            actor=_platform_actor_key(request),
            detail={"path": request.url.path},
        )
    except Exception:  # noqa: BLE001 - 审计是旁路
        logger.exception("审计写入失败 event=%s", event)


@app.middleware("http")
async def _platform_auth(request, call_next):
    """D25/D26：/v1 的 Bearer 认证 + 进程内限流。

    配了 `PERSONAL_AGENT_PLATFORM_TOKEN` 就强制校验；没配则放行并在启动时
    打警告 —— 未配置意味着「只监听本机」这条边界还在生效。
    `PERSONAL_AGENT_PLATFORM_RATE_PER_MIN=0` 显式关闭限流（测试与受控环境）。
    """

    if not request.url.path.startswith("/v1"):
        return await call_next(request)

    key = _platform_actor_key(request)
    limiter = get_limiter()

    # 审计查询要能在事故中随取随用，不占限流额度（认证仍然要过）。
    if (
        limiter is not None
        and not request.url.path.startswith("/v1/audit")
        and not limiter.allow(key)
    ):
        _audit_denied(request, "rate_limited", "RATE_LIMITED")
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

    token = platform_token()

    if token is None:
        return await call_next(request)

    header = request.headers.get("authorization", "")
    scheme, _, provided = header.partition(" ")

    if scheme.lower() != "bearer" or not hmac.compare_digest(
        provided.strip(), token
    ):
        _audit_denied(request, "auth.denied", "UNAUTHORIZED")
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

# Bot 试听与既有语音播放（独立于旧聊天室）
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

# 团队任务分配（与聊天室并行）
app.include_router(teams_router)


# ==========================================
# 桌面版：兼容 /api 前缀
# ==========================================
#
# 开发时是 Vite 代理把 `/api/*` 转成 `/*` 再打给后端；
# 打包成桌面应用后没有代理，前端照样请求 `/api/bots`，
# 直接打后端就 404。这里把前缀剥掉，让两种模式共用同一套路由。
@app.middleware("http")
async def _strip_api_prefix(request, call_next):
    path = request.url.path

    is_api = path.startswith("/api/")
    if is_api:
        stripped = path[4:]
        request.scope["path"] = stripped
        request.scope["raw_path"] = stripped.encode("utf-8")
        # SPA 回退不能吞掉 /api/*：标记一下，让兜底路由继续返回 404。
        request.scope["_was_api_prefix"] = True

    response = await call_next(request)
    # 未知 /api 路径（含方法不匹配）统一 404，不要落到前端回退去变成 405/页面。
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