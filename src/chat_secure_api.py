"""Personal Agent 已认证聊天接口。

功能：
- 登录认证与聊天数据隔离
- 请求幂等保护
- 每用户次数及并发限制
- 全站滚动24小时 US$1 预算准入
- 完整 Token 用量的保守费用结算
- 提醒工具绑定服务端认证身份

不允许浏览器自行指定提醒所属用户。
"""

import sqlite3
import uuid

from contextlib import closing
from urllib.parse import urlsplit

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Response,
)
from fastapi.responses import JSONResponse

from src import agent
from src import api as legacy_chat
from src.auth_api import get_current_user

from src.core.ai_budget import (
    BudgetExceeded,
    init_budget_schema,
    settle_budget,
)
from src.core.ai_budget_reconcile import (
    reconcile_recorded_request,
)
from src.core.ai_pricing import PricingUnavailable
from src.core.chat_admission import admit_chat_request

from src.core.chat_ownership import (
    RequestMessageConflict,
    RequestOwnerMismatch,
    read_owned_record,
    update_owned_status,
)
from src.core.chat_quota import (
    ChatConcurrencyLimitExceeded,
    ChatDailyLimitExceeded,
)

from src.core.reminder_identity import (
    bind_authenticated_reminder,
)


router = APIRouter(
    tags=["已认证聊天"],
)

NO_STORE = {
    "Cache-Control": "no-store",
}


# ==========================================
# 模型服务商配置
# ==========================================

def _provider_host() -> str:
    """读取服务端模型配置，不接受浏览器报价。"""

    try:
        return (
            urlsplit(
                str(agent.client.base_url)
            ).hostname
            or ""
        )

    except (TypeError, ValueError):
        return ""


# ==========================================
# 聊天记录读取
# ==========================================

def _read(
    request_id: str,
    user_id: str,
) -> dict | None:

    with closing(
        legacy_chat.connect_db()
    ) as conn:

        return read_owned_record(
            conn,
            request_id,
            user_id,
        )


# ==========================================
# 原子准入
# ==========================================

def _admit(
    request_id: str,
    digest: str,
    user_id: str,
    role: str,
) -> dict:

    with closing(
        legacy_chat.connect_db()
    ) as conn:

        init_budget_schema(conn)

        return admit_chat_request(
            conn,
            request_id=request_id,
            digest=digest,
            user_id=user_id,
            role=role,
            provider_host=_provider_host(),
            model=agent.MODEL,
        )


# ==========================================
# 更新聊天状态
# ==========================================

def _update(
    request_id: str,
    user_id: str,
    status: str,
    reply: str | None = None,
) -> bool:

    with closing(
        legacy_chat.connect_db()
    ) as conn:

        return update_owned_status(
            conn,
            request_id,
            user_id,
            status,
            reply,
            legacy_chat.now_utc(),
        )


# ==========================================
# 费用未知时保留预算
# ==========================================

def _retain_budget(
    request_id: str,
    user_id: str,
) -> None:

    try:
        with closing(
            legacy_chat.connect_db()
        ) as conn:

            settle_budget(
                conn,
                request_id=request_id,
                user_id=user_id,
                actual_usd=None,
            )

    except Exception:
        legacy_chat.logger.exception(
            "Failed retaining budget request_id=%s",
            request_id,
        )


# ==========================================
# 已完成聊天的费用核对
# ==========================================

def _reconcile_completed_budget(
    request_id: str,
    user_id: str,
    completed_calls: int,
) -> None:

    if (
        type(completed_calls) is not int
        or completed_calls not in (1, 2)
    ):
        _retain_budget(
            request_id,
            user_id,
        )
        return

    try:
        state = reconcile_recorded_request(
            budget_db_path=legacy_chat.CHAT_DB_PATH,
            usage_db_path=agent.USAGE_DB_PATH,
            request_id=request_id,
            user_id=user_id,
            provider_host=_provider_host(),
            model=agent.MODEL,
            completed=True,
            expected_calls=completed_calls,
        )

        if state == "uncertain":
            legacy_chat.logger.warning(
                "Incomplete usage; budget retained "
                "request_id=%s",
                request_id,
            )

        elif state == "overrun":
            legacy_chat.logger.error(
                "Model cost exceeded reservation "
                "request_id=%s",
                request_id,
            )

    except Exception:
        legacy_chat.logger.exception(
            "Budget reconciliation failed "
            "request_id=%s",
            request_id,
        )


# ==========================================
# 统一拒绝响应
# ==========================================

def _rejected(
    request_id: str,
    *,
    code: str,
    detail: str,
    retry_after: str | None = None,
) -> JSONResponse:

    headers = dict(NO_STORE)

    if retry_after is not None:
        headers["Retry-After"] = retry_after

    return JSONResponse(
        status_code=429,
        headers=headers,
        content={
            "request_id": request_id,
            "status": "rejected",
            "code": code,
            "detail": detail,
        },
    )


# ==========================================
# 查询聊天请求
# ==========================================

@router.get("/chat/requests/{request_id}")
def get_chat_request(
    request_id: str,
    response: Response,
    user: dict = Depends(get_current_user),
):

    response.headers["Cache-Control"] = "no-store"

    legacy_chat.validate_request_id(request_id)

    record = _read(
        request_id,
        user["id"],
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="请求不存在",
        )

    return {
        "request_id": request_id,
        "status": record["status"],
        "reply": (
            record["reply"]
            if record["status"] == "completed"
            else None
        ),
    }


# ==========================================
# 正式聊天接口
# ==========================================

@router.post("/chat")
def chat_endpoint(
    request: legacy_chat.ChatRequest,
    response: Response,
    user: dict = Depends(get_current_user),
):

    response.headers["Cache-Control"] = "no-store"

    message = request.message.strip()

    # 输入不合法时不能领取请求或占用预算。
    try:
        agent.validate_user_input(message)

    except ValueError as exc:
        raise HTTPException(
            status_code=413,
            detail=str(exc),
        ) from None

    request_id = (
        request.request_id
        or uuid.uuid4().hex
    )

    legacy_chat.validate_request_id(request_id)

    # 身份必须来自 FastAPI 的认证依赖，
    # 不能从浏览器提交的消息里提取。
    user_id = user["id"]
    role = user["role"]

    # ======================================
    # 身份、次数、并发与预算准入
    # ======================================

    try:
        admission = _admit(
            request_id,
            legacy_chat.message_digest(message),
            user_id,
            role,
        )

    except RequestOwnerMismatch:
        raise HTTPException(
            status_code=404,
            detail="请求不存在",
        ) from None

    except RequestMessageConflict:
        raise HTTPException(
            status_code=409,
            detail=(
                "同一 request_id 不能对应不同消息"
            ),
        ) from None

    except ChatDailyLimitExceeded:
        return _rejected(
            request_id,
            code="daily_quota_exceeded",
            detail="过去24小时的新请求次数已达到上限",
        )

    except ChatConcurrencyLimitExceeded:
        return _rejected(
            request_id,
            code="concurrency_limit_exceeded",
            detail="正在处理的请求过多，请稍后再试",
            retry_after="15",
        )

    except BudgetExceeded:
        return _rejected(
            request_id,
            code="global_budget_exceeded",
            detail=(
                "全站过去24小时 AI 预算达到 "
                "US$1 上限，请稍后再试"
            ),
        )

    except PricingUnavailable:
        return _rejected(
            request_id,
            code="pricing_unavailable",
            detail=(
                "当前模型没有经过核实的价格配置，"
                "已暂停新的 AI 请求"
            ),
        )

    # ======================================
    # 已有请求不再调用模型或创建提醒
    # ======================================

    if not admission["admitted"]:

        if admission["status"] == "completed":
            return {
                "request_id": request_id,
                "status": "completed",
                "reply": admission["reply"],
            }

        if admission["status"] == "processing":
            return JSONResponse(
                status_code=202,
                headers=NO_STORE,
                content={
                    "request_id": request_id,
                    "status": "processing",
                    "detail": "请求仍在处理中",
                },
            )

        return JSONResponse(
            status_code=409,
            headers=NO_STORE,
            content={
                "request_id": request_id,
                "status": "unknown",
                "detail": (
                    "原请求结果无法确认，"
                    "请勿重新发送相同请求"
                ),
            },
        )

    # ======================================
    # 新请求：执行模型
    # ======================================

    try:

        # 新增的关键身份绑定。
        #
        # user_id / role 来自 get_current_user。
        # request_id 来自已通过准入检查的聊天。
        #
        # 整个模型和工具执行过程都在上下文内。
        # 因此 agent.py 调用提醒工具时，
        # 可以安全读取本次登录身份。
        #
        # 执行结束或抛出异常后，
        # reminder_identity 会自动清理上下文。

        with bind_authenticated_reminder(
            user_id=user_id,
            request_id=request_id,
            role=role,
        ):

            with agent.track_chat_usage(
                user_id,
                request_id,
            ) as usage_scope:

                reply = legacy_chat.chat(message)

        if (
            not isinstance(reply, str)
            or not reply.strip()
        ):
            raise ValueError(
                "模型没有返回有效文本"
            )

    except Exception:
        legacy_chat.logger.exception(
            "Model failed request_id=%s",
            request_id,
        )

        # 模型出错时不释放预算。
        _retain_budget(
            request_id,
            user_id,
        )

        try:
            _update(
                request_id,
                user_id,
                "unknown",
            )

        except sqlite3.Error:
            legacy_chat.logger.exception(
                "Failed saving unknown request_id=%s",
                request_id,
            )

        return JSONResponse(
            status_code=502,
            headers=NO_STORE,
            content={
                "request_id": request_id,
                "status": "unknown",
                "detail": (
                    "模型调用结果无法确认，"
                    "请勿直接重新发送"
                ),
            },
        )

    # ======================================
    # 先保存聊天回复，再核对费用
    # ======================================

    try:
        saved = _update(
            request_id,
            user_id,
            "completed",
            reply,
        )

    except sqlite3.Error:
        legacy_chat.logger.exception(
            "Failed saving reply request_id=%s",
            request_id,
        )

        _retain_budget(
            request_id,
            user_id,
        )

        return JSONResponse(
            status_code=503,
            headers=NO_STORE,
            content={
                "request_id": request_id,
                "status": "unknown",
                "detail": (
                    "模型回复保存失败，"
                    "请查询原请求"
                ),
            },
        )

    if not saved:
        _retain_budget(
            request_id,
            user_id,
        )

        legacy_chat.logger.error(
            "Request state changed request_id=%s",
            request_id,
        )

        return JSONResponse(
            status_code=409,
            headers=NO_STORE,
            content={
                "request_id": request_id,
                "status": "unknown",
                "detail": (
                    "请求状态发生变化，"
                    "请查询原请求"
                ),
            },
        )

    # 聊天回复保存成功后，
    # 才核对所有已收到模型响应的用量。
    _reconcile_completed_budget(
        request_id,
        user_id,
        usage_scope.completed_calls,
    )

    return {
        "request_id": request_id,
        "status": "completed",
        "reply": reply,
    }