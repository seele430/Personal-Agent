"""Personal Agent 正式后端入口。

负责：
- 初始化聊天数据库
- 初始化用户归属及 AI 预算表
- 注册认证、聊天和个人提醒路由
"""

from contextlib import asynccontextmanager, closing

from fastapi import FastAPI

from src import api as legacy_chat
from src.auth_api import router as auth_router
from src.bot_api import router as bot_router
from src.chat_secure_api import router as chat_router
from src.inbox_api import router as inbox_router
from src.monitors_api import router as monitor_router
from src.notifications_api import router as notification_router
from src.reminder_api import router as reminder_router
from src.tts_api import router as tts_router
from src.teams_api import router as teams_router
from src.usage_api import router as usage_router

from src.core.chat_ownership import migrate_chat_ownership
from src.core.ai_budget import init_budget_schema
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

    yield


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


@app.get("/health")
def health():
    return {
        "status": "ok",
    }


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

# 个人站内提醒查询
app.include_router(reminder_router)

# 个人额度查询（只读）
app.include_router(usage_router)

# 监控任务（按归属隔离，全局调度）
app.include_router(monitor_router)

# 站内通知（非站长用户的投递渠道）
app.include_router(notification_router)

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

    if path.startswith("/api/"):
        stripped = path[4:]
        request.scope["path"] = stripped
        request.scope["raw_path"] = stripped.encode("utf-8")

    return await call_next(request)


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
        app.mount(
            "/", StaticFiles(directory=dist, html=True), name="frontend"
        )


_mount_frontend()