
"""Personal Agent 统一 API 启动入口。"""

from src.api import app
from src.auth_api import router as auth_router


# 在保留原有聊天接口的基础上，
# 注册新的用户认证接口。
app.include_router(auth_router)