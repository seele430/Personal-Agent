"""Bot 试听与既有语音播放共用的 Edge TTS 接口。

该模块独立于旧聊天室路由；桌面版仍通过 edge_tts 子进程合成。
"""

import logging
import os
import subprocess
import sys
import tempfile
import time

import edge_tts  # noqa: F401 - 保留桌面打包所需的模块依赖
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.core.trace_context import span


router = APIRouter(prefix="/api", tags=["语音"])
logger = logging.getLogger("personal_agent.tts")


# 音色 key → Edge TTS 的 voice 名（两男两女，都是神经网络语音）
EDGE_VOICES = {
    "female-1": "zh-CN-XiaoxiaoNeural",
    "female-2": "zh-CN-XiaoyiNeural",
    "male-1": "zh-CN-YunxiNeural",
    "male-2": "zh-CN-YunjianNeural",
}


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    voice: str = Field(default="female-1", max_length=30)
    rate: str = Field(default="+20%", max_length=20)


@router.get("/tts/diag")
def tts_diag():
    """诊断后端进程连接 Edge TTS 端点的能力。"""

    import socket
    import urllib.request

    result = {
        "cwd": os.getcwd(),
        "executable": sys.executable,
        "proxies": urllib.request.getproxies(),
        "no_proxy": os.environ.get("NO_PROXY"),
    }

    try:
        probe = subprocess.run(
            [sys.executable, "-c", "print('child-ok')"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        result["child"] = probe.stdout.decode(
            "utf-8", "replace"
        ).strip() or f"rc={probe.returncode}"
    except Exception as error:  # noqa: BLE001
        result["child"] = f"{type(error).__name__}: {error}"

    try:
        data = _synthesize(
            "你好，世界。", "zh-CN-XiaoxiaoNeural", "+12%"
        )
        result["synth"] = f"ok {len(data)} bytes"
    except Exception as error:  # noqa: BLE001
        result["synth"] = f"{type(error).__name__}: {str(error)[:200]}"

    try:
        socket.create_connection(
            ("speech.platform.bing.com", 443), timeout=8
        ).close()
        result["tcp_443"] = "ok"
    except Exception as error:  # noqa: BLE001
        result["tcp_443"] = f"{type(error).__name__}: {error}"

    try:
        with urllib.request.urlopen(
            "https://speech.platform.bing.com", timeout=8
        ) as resp:
            result["https"] = f"ok {resp.status}"
    except Exception as error:  # noqa: BLE001
        result["https"] = f"{type(error).__name__}: {error}"

    return result


def _synthesize_once(text: str, voice: str, rate: str) -> bytes:
    """在独立子进程运行 edge-tts CLI，避免 uvicorn 内合成空音频。

    中文文本通过 UTF-8 临时文件传入，避免 Windows argv 编码问题。
    """

    media = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    media.close()

    script = tempfile.NamedTemporaryFile(
        suffix=".txt", delete=False, mode="w", encoding="utf-8"
    )

    # Edge TTS 对以中文开头的文本可能返回空音频；零宽空格不会被朗读。
    with script:
        script.write("\u200b" + text)

    try:
        env = {
            **os.environ,
            "NO_PROXY": "*",
            "no_proxy": "*",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }

        err_log = tempfile.NamedTemporaryFile(suffix=".err", delete=False)
        err_log.close()

        with open(err_log.name, "wb") as err_handle:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "edge_tts",
                    "--file",
                    script.name,
                    "--voice",
                    voice,
                    "--rate",
                    rate,
                    "--write-media",
                    media.name,
                    "--no-progress",
                ],
                stdout=subprocess.DEVNULL,
                stderr=err_handle,
                timeout=25,
                check=False,
                env=env,
                stdin=subprocess.DEVNULL,
            )

        if completed.returncode != 0:
            with open(err_log.name, "rb") as handle:
                detail = handle.read().decode(
                    "utf-8", errors="replace"
                )[-400:]

            raise RuntimeError(
                f"rc={completed.returncode} err={detail!r}"
            )

        with open(media.name, "rb") as handle:
            return handle.read()

    finally:
        for path in (media.name, script.name):
            try:
                os.unlink(path)
            except OSError:
                pass


def _synthesize(text: str, voice: str, rate: str) -> bytes:
    """对 Edge TTS 的临时限流错误进行最多三次合成。"""

    last_error: Exception | None = None

    for attempt in range(3):
        try:
            return _synthesize_once(text, voice, rate)

        except Exception as error:  # noqa: BLE001
            last_error = error

            if attempt < 2:
                logger.warning(
                    "TTS 合成失败（第 %s 次），%.1fs 后重试：%s",
                    attempt + 1,
                    1.5 * (attempt + 1),
                    error,
                )
                time.sleep(1.5 * (attempt + 1))

    raise last_error if last_error else RuntimeError("语音合成失败")


@router.post("/tts")
def text_to_speech(payload: TTSRequest):
    """把一段文字合成为 mp3。"""

    voice = EDGE_VOICES.get(payload.voice, EDGE_VOICES["female-1"])

    try:
        with span("tts.synth", voice=voice):
            audio = _synthesize(payload.text, voice, payload.rate)

    except Exception as error:  # noqa: BLE001
        logger.exception("Edge TTS 合成失败 voice=%s", voice)
        raise HTTPException(
            status_code=502, detail=f"语音合成失败：{error}"
        ) from None

    if not audio:
        raise HTTPException(status_code=502, detail="语音合成结果为空")

    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )
