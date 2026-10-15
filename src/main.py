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
from src.chat_secure_api import router as chat_router
from src.monitors_api import router as monitor_router
from src.reminder_api import router as reminder_router
from src.usage_api import router as usage_router

from src.core.chat_ownership import migrate_chat_ownership
from src.core.ai_budget import init_budget_schema


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

# 已认证聊天、额度及费用保护
app.include_router(chat_router)

# 个人站内提醒查询
app.include_router(reminder_router)

# 个人额度查询（只读）
app.include_router(usage_router)

# 监控任务（按归属隔离，全局调度）
app.include_router(monitor_router)