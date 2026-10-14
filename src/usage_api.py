"""个人额度查询接口（只读）。

只是把额度账本读出来给用户看，不做任何预留、结算或修改。
真正拦截请求的逻辑仍在 chat_admission / chat_secure_api。
"""

from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException, Response

from src import api as legacy_chat
from src.auth_api import get_current_user
from src.core.quota_report import build_quota_report


router = APIRouter(tags=["个人额度"])


@router.get("/usage/summary")
def get_usage_summary(
    response: Response,
    user: dict = Depends(get_current_user),
):
    """当前用户过去 24 小时的额度占用。"""

    # 个人用量不得由浏览器或代理缓存。
    response.headers["Cache-Control"] = "no-store"

    # 身份只来自服务端会话。
    user_id = user["id"]
    role = user.get("role")

    try:
        with closing(
            legacy_chat.connect_db()
        ) as conn:
            return build_quota_report(
                conn,
                user_id=user_id,
                role=role,
            )

    except ValueError as error:
        # 未知角色等脏数据：明确拒绝，不猜一个额度。
        raise HTTPException(
            status_code=403,
            detail=str(error),
        ) from None
