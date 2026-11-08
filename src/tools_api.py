"""ComfyUI 图生图的 HTTP 入口（供前端调用）。

错误码约定（单测钉住，别混）：

- 400：参数不合法（空 prompt / steps 越界 / denoise 越界）；
- 401/403：未登录（由 auth 依赖给出）；
- 502：ComfyUI 执行失败 / 超时 / 返回结构不对；
- 503：ComfyUI **没在运行**（消息里带"未运行"）。

本端点**不在远程只读白名单**里：远程访问仍然 403，这是刻意的——
图生图会占 GPU、写文件，不该由远程只读端触发。
"""

from __future__ import annotations

import json

from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException

from src.auth_api import get_current_user
from src.tools import comfy_tools


router = APIRouter(prefix="/api/tools/comfy", tags=["工具"])


def _tool_call(arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        function=SimpleNamespace(
            name="comfy_img2img",
            arguments=json.dumps(arguments, ensure_ascii=False),
        )
    )


@router.post("/img2img")
def img2img(payload: dict, user: dict = Depends(get_current_user)) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")

    if not str(payload.get("prompt") or "").strip():
        raise HTTPException(status_code=400, detail="prompt 不能为空")

    if not str(payload.get("image_path") or "").strip():
        raise HTTPException(status_code=400, detail="image_path 不能为空")

    try:
        result = comfy_tools.comfy_img2img(_tool_call(payload))

    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None

    except RuntimeError as error:
        message = str(error)

        # 503 = 服务当前不可用（没启动 / 显存不够 / 已有任务在跑），不是执行失败
        if "未运行" in message:
            raise HTTPException(
                status_code=503,
                detail=f"{message}（提示：先运行 ComfyUI，再试）",
            ) from None

        if "显存不足" in message or "已有出图任务" in message:
            raise HTTPException(status_code=503, detail=message) from None

        raise HTTPException(status_code=502, detail=message) from None

    return {
        "success": True,
        "prompt_id": result.get("prompt_id"),
        "images": result.get("images") or [],
        "path": result.get("path"),
    }
