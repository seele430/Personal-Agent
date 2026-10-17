"""聊天室接口（S4c）。

- 创建房间 / 列房间 / 拉 Bot / 列成员 / 发消息触发发言 / 拉消息流；
- `user_id` 一律取自服务端会话；
- 发消息会真实调用模型让各 Bot 发言（走 `room_service.run_room_turn`）。
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid

import edge_tts
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.auth_api import get_current_user
from src.core.room_service import (
    ask_bots as ask_bots_service,
    approve_and_expand,
    bot_join_opening,
    host_summarize_turn,
    run_free_turn,
    run_opening,
    run_room_turn,
    run_stage,
)
from src.core.room_store import RoomError, RoomStore


router = APIRouter(tags=["聊天室"])
logger = logging.getLogger("personal_agent.room")


# 音色 key → Edge TTS 的 voice 名（两男两女，都是神经网络语音）
EDGE_VOICES = {
    "female-1": "zh-CN-XiaoxiaoNeural",   # 晓晓：自然、通用
    "female-2": "zh-CN-XiaoyiNeural",     # 晓伊：年轻、偏活泼
    "male-1": "zh-CN-YunxiNeural",        # 云希：阳光
    "male-2": "zh-CN-YunjianNeural",      # 云健：低沉有力
}


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    voice: str = Field(default="female-1", max_length=30)
    # 默认语速：+20% 比云端默认快一档，听着不拖
    rate: str = Field(default="+20%", max_length=20)


@router.get("/tts/diag")
def tts_diag():
    """诊断：后端进程能不能连上 Edge TTS 的端点。"""

    import socket
    import urllib.request

    result = {
        "cwd": os.getcwd(),
        "executable": sys.executable,
        "proxies": urllib.request.getproxies(),
        "no_proxy": os.environ.get("NO_PROXY"),
    }

    # 在 API 进程里跑一次子进程，看它能不能正常执行
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

    # 再跑一次真正的合成（直接调用内部函数）
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
    """单次合成：在**独立子进程**里跑 edge-tts CLI。

    两个坑（都是实测踩出来的）：
    1. 在 uvicorn 的进程里跑 edge-tts（无论哪个 loop）都会拿到空音频
       （NoAudioReceived）—— 所以起子进程；
    2. **中文不能走命令行参数**：Windows 上 subprocess 传中文 argv 会坏掉，
       Edge 服务端就回空音频（"Hello world" 能成、"你好" 就不行）。
       所以文本写进 UTF-8 临时文件，用 `--file` 传。
    """

    media = tempfile.NamedTemporaryFile(
        suffix=".mp3", delete=False
    )
    media.close()

    script = tempfile.NamedTemporaryFile(
        suffix=".txt", delete=False, mode="w", encoding="utf-8"
    )

    # Edge TTS 的坑：**以中文开头**的文本会返回空音频（NoAudioReceived）。
    # 加一个零宽空格前缀就能绕过，而且它是不可见字符，不会被念出来。
    # 实测："你好" 挂 → "\u200b你好" 成；长中文同样适用。
    with script:
        script.write("\u200b" + text)

    try:
        env = {
            **os.environ,
            # 本机系统代理是死配置（ProxyEnable=1 指向 127.0.0.1:7890 但没进程），
            # 不绕过的话 edge-tts 会连到空端口 → NoAudioReceived
            "NO_PROXY": "*",
            "no_proxy": "*",
            # 中文文本的编码：不强制 UTF-8 的话子进程可能按 cp936 读，
            # Edge 服务端拿到乱码就回空音频
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }

        err_log = tempfile.NamedTemporaryFile(
            suffix=".err", delete=False
        )
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
                # 单次超时不要太长：503 限流时 CLI 会一直挂着，
                # 60 秒 × 3 次重试 = 用户干等三分钟。25 秒足够合成正常文本。
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
    """带重试的合成。

    Edge TTS 会偶发 `WSServerHandshakeError: 503`（`Sec-MS-GEC` 令牌被拒，
    多半是短时间请求过多触发了限流）。实测隔一会儿重试就能过，
    所以这里重试 3 次、逐次加长间隔，比直接报错让用户重来更友好。
    """

    last_error: Exception | None = None

    for attempt in range(3):
        try:
            return _synthesize_once(text, voice, rate)

        except Exception as error:  # noqa: BLE001 - 要拿到最后一次错误
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
    """把一段文字读成 mp3（Edge 神经网络 TTS，免 key）。

    语速默认 +12% —— 默认语速偏慢，听着拖。
    """

    voice = EDGE_VOICES.get(payload.voice, EDGE_VOICES["female-1"])

    try:
        audio = _synthesize(payload.text, voice, payload.rate)

    except Exception as error:  # noqa: BLE001 - 合成失败要原样回给前端
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


def _open_store() -> RoomStore:
    return RoomStore()


def _not_found() -> HTTPException:
    # 不属于自己和不存在返回同一个响应，避免探测。
    return HTTPException(
        status_code=404,
        detail="房间不存在或无权访问",
    )


class RoomCreate(BaseModel):
    name: str
    stances: list[str] | None = None
    host_bot_id: int | None = None


class BotAdd(BaseModel):
    bot_id: int
    stance: str | None = None
    # 进房就立论（默认开；主持人这类没立场的自然不产出）
    opening: bool = True


class MessageSend(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    word_limit: int = Field(default=80, ge=10, le=200)


@router.post("/rooms")
def create_room(
    payload: RoomCreate,
    user: dict = Depends(get_current_user),
):
    try:
        return _open_store().create_room(
            owner_id=user["id"],
            name=payload.name,
            stances=payload.stances,
            host_bot_id=payload.host_bot_id,
        )

    except RoomError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None


@router.get("/rooms")
def list_rooms(user: dict = Depends(get_current_user)):
    return {"rooms": _open_store().list_rooms(user["id"])}


class RoomArchive(BaseModel):
    archived: bool = True


@router.post("/rooms/{room_id}/archive")
def archive_room(
    room_id: int,
    payload: RoomArchive,
    user: dict = Depends(get_current_user),
):
    """归档 / 取消归档房间（软隐藏，不删数据）。"""

    try:
        ok = _open_store().archive_room(
            room_id=room_id,
            owner_id=user["id"],
            archived=payload.archived,
        )

    except RoomError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None

    if not ok:
        raise _not_found() from None

    return {"archived": payload.archived}


@router.delete("/rooms/{room_id}")
def delete_room(
    room_id: int,
    user: dict = Depends(get_current_user),
):
    """删除房间（级联清成员 / 消息 / 举手）。"""

    try:
        ok = _open_store().delete_room(
            room_id=room_id, owner_id=user["id"]
        )

    except RoomError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None

    if not ok:
        raise _not_found() from None

    return {"deleted": True}


@router.post("/rooms/{room_id}/bots")
def add_bot_to_room(
    room_id: int,
    payload: BotAdd,
    user: dict = Depends(get_current_user),
):
    store = _open_store()

    try:
        store.add_bot_to_room(
            room_id=room_id,
            bot_id=payload.bot_id,
            owner_id=user["id"],
            stance=payload.stance,
        )

    except RoomError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None

    # 定了立场就能发言：进房即做开篇立论
    opening = ""

    if payload.opening:
        try:
            opening = bot_join_opening(
                room_store=store,
                room_id=room_id,
                bot_id=payload.bot_id,
                owner_id=user["id"],
            )

        except ValueError:
            opening = ""

    return {"added": True, "opening": opening}


@router.post("/rooms/{room_id}/opening")
def run_room_opening(
    room_id: int,
    user: dict = Depends(get_current_user),
):
    """开场：主持人先立规矩，然后各辩手依次开篇立论。"""

    store = _open_store()

    try:
        return run_opening(
            room_store=store,
            room_id=room_id,
            owner_id=user["id"],
        )

    except (RoomError, ValueError):
        raise _not_found() from None


class StageAdvance(BaseModel):
    stage: str = Field(min_length=1, max_length=20)


@router.post("/rooms/{room_id}/turn")
def free_turn(
    room_id: int,
    user: dict = Depends(get_current_user),
):
    """交锋的一个回合：轮到谁谁说话（回合制，一次一条）。"""

    try:
        return run_free_turn(
            room_store=_open_store(),
            room_id=room_id,
            owner_id=user["id"],
        )

    except (RoomError, ValueError) as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None


@router.post("/rooms/{room_id}/advance")
def advance_room_stage(
    room_id: int,
    payload: StageAdvance,
    user: dict = Depends(get_current_user),
):
    """推进辩论阶段，并生成该阶段内容。

    复盘（review）交给主持人汇总，其余阶段走 `run_stage`。
    """

    store = _open_store()

    try:
        if payload.stage == "review":
            # 复盘汇总**整场**，不按 turn 过滤：
            # 投票那类 turn 里只有用户发言，按 turn 汇总会得到空记录直接报错。
            store.advance_stage(
                room_id=room_id, owner_id=user["id"], stage="review"
            )

            return host_summarize_turn(
                room_store=store,
                room_id=room_id,
                owner_id=user["id"],
                turn_id=None,
            )

        # 立论之后的环节都需要「双方」在场：一个 Bot 没法驳论、质询、交锋
        needs_debate = {"rebuttal", "crossfire", "free", "closing", "vote"}

        if payload.stage in needs_debate:
            room = store.get_room(
                room_id=room_id, owner_id=user["id"]
            )

            if room is None:
                raise HTTPException(
                    status_code=404, detail="房间不存在"
                )

            members = store.list_room_bots(
                room_id=room_id, owner_id=user["id"]
            )

            debaters = [
                item
                for item in members
                if item["id"] != room.get("host_bot_id")
                and item.get("stance")
            ]

            if len(debaters) < 2:
                raise HTTPException(
                    status_code=409,
                    detail="至少需要两名有立场的 Bot 才能进入这一阶段"
                    "（先在「立论」阶段把另一边拉进来）",
                )

            # 光有人数不够：两边都站同一边，驳论/质询/交锋根本无从谈起
            sides = {item["stance"] for item in debaters}

            if len(sides) < 2:
                raise HTTPException(
                    status_code=409,
                    detail="两边的 Bot 站在同一立场，没法辩论"
                    "（至少要有一个站在对立面）",
                )

        return run_stage(
            room_store=store,
            room_id=room_id,
            owner_id=user["id"],
            stage=payload.stage,
        )

    except (RoomError, ValueError) as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None


class BotScore(BaseModel):
    bot_id: int
    score: int = Field(ge=0, le=10)


class ScoreSubmit(BaseModel):
    scores: list[BotScore]


@router.post("/rooms/{room_id}/scores")
def submit_scores(
    room_id: int,
    payload: ScoreSubmit,
    user: dict = Depends(get_current_user),
):
    """给辩手打分（0-10）。

    权重预留给多人场景：**房主 1.5 票，其他人类 1 票**。
    目前只有房主能提交，所以权重恒为 1.5。
    """

    store = _open_store()
    weight = 1.5

    results = []

    for item in payload.scores:
        try:
            results.append(
                store.submit_score(
                    room_id=room_id,
                    bot_id=item.bot_id,
                    voter_id=user["id"],
                    score=item.score,
                    weight=weight,
                )
            )

        except RoomError as error:
            raise HTTPException(
                status_code=422, detail=str(error)
            ) from None

    return {"scores": results, "weight": weight}


@router.get("/rooms/{room_id}/scores")
def list_room_scores(
    room_id: int,
    user: dict = Depends(get_current_user),
):
    """房间的加权评分汇总（按 Bot）。"""

    try:
        return {
            "scores": _open_store().list_scores(
                room_id=room_id, owner_id=user["id"]
            )
        }

    except RoomError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None


class VoteSubmit(BaseModel):
    stance: str = Field(min_length=1, max_length=100)


@router.post("/rooms/{room_id}/vote")
def submit_vote(
    room_id: int,
    payload: VoteSubmit,
    user: dict = Depends(get_current_user),
):
    """评委投票（选一边）。房主 1.5 票，其他人 1 票。

    同时往聊天流里发一条「我支持 X」，让投票在讨论里留痕。
    """

    store = _open_store()
    weight = 1.5  # 房主

    try:
        vote = store.submit_vote(
            room_id=room_id,
            voter_id=user["id"],
            stance=payload.stance,
            weight=weight,
        )

        store.add_message(
            room_id=room_id,
            turn_id=uuid.uuid4().hex,
            speaker="user",
            bot_id=None,
            role="core",
            stage="vote",
            content=f"我支持「{payload.stance}」",
            owner_id=user["id"],
        )

    except RoomError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None

    return {"vote": vote}


@router.get("/rooms/{room_id}/votes")
def list_room_votes(
    room_id: int,
    user: dict = Depends(get_current_user),
):
    """投票汇总（按立场加权）。"""

    try:
        return _open_store().list_votes(
            room_id=room_id, owner_id=user["id"]
        )

    except RoomError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None


class LeanAsk(BaseModel):
    bot_id: int


@router.post("/rooms/{room_id}/lean")
def ask_bot_lean(
    room_id: int,
    payload: LeanAsk,
    user: dict = Depends(get_current_user),
):
    """问某个 Bot 更倾向哪一边：只回一个立场，不写论述。"""

    from src import agent
    from src.core.bot_store import BotStore

    store = _open_store()
    room = store.get_room(room_id=room_id, owner_id=user["id"])

    if room is None:
        raise HTTPException(status_code=404, detail="房间不存在")

    bot = BotStore().get_bot(payload.bot_id)

    if bot is None:
        raise HTTPException(status_code=404, detail="Bot 不存在")

    try:
        lean = agent.bot_lean(
            bot, room["name"], room.get("stances") or []
        )

    except Exception as error:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"判断失败：{error}"
        ) from None

    return {"lean": lean}


@router.get("/rooms/{room_id}/bots")
def list_room_bots(
    room_id: int,
    user: dict = Depends(get_current_user),
):
    try:
        bots = _open_store().list_room_bots(
            room_id=room_id, owner_id=user["id"]
        )

    except RoomError:
        raise _not_found() from None

    return {"bots": bots}


@router.post("/rooms/{room_id}/messages")
def send_message(
    room_id: int,
    payload: MessageSend,
    user: dict = Depends(get_current_user),
):
    store = _open_store()

    try:
        result = run_room_turn(
            room_store=store,
            room_id=room_id,
            owner_id=user["id"],
            message=payload.message,
            word_limit=payload.word_limit,
        )

    except RoomError:
        raise _not_found() from None

    return result


class AskRequest(BaseModel):
    bot_id: int | None = None
    word_limit: int = Field(default=80, ge=10, le=200)


@router.post("/rooms/{room_id}/ask")
def ask_room_bots(
    room_id: int,
    payload: AskRequest,
    user: dict = Depends(get_current_user),
):
    """「问问 Bot 怎么想」：把**房间议题**抛给 Bot，不记用户消息。

    传 bot_id 只问那一个，不传问房里全部。
    """

    store = _open_store()

    try:
        result = ask_bots_service(
            room_store=store,
            room_id=room_id,
            owner_id=user["id"],
            bot_id=payload.bot_id,
            word_limit=payload.word_limit,
        )

    except RoomError:
        raise _not_found() from None

    return result


@router.get("/rooms/{room_id}/messages")
def list_messages(
    room_id: int,
    user: dict = Depends(get_current_user),
):
    try:
        messages = _open_store().list_messages(
            room_id=room_id, owner_id=user["id"]
        )

    except RoomError:
        raise _not_found() from None

    return {"messages": messages}


class HandRaise(BaseModel):
    bot_id: int
    turn_id: str
    kind: str = "expand"
    reason: str = Field(min_length=1, max_length=200)


class HandReject(BaseModel):
    unfair: bool = False


class Decision(BaseModel):
    option: str = Field(min_length=1, max_length=200)
    # 结算活跃值：站对边的不扣，其余扣这么多（默认 30）
    penalty: int = Field(default=30, ge=0, le=100)


@router.post("/rooms/{room_id}/hands")
def raise_hand(
    room_id: int,
    payload: HandRaise,
    user: dict = Depends(get_current_user),
):
    try:
        return _open_store().raise_hand(
            room_id=room_id,
            turn_id=payload.turn_id,
            bot_id=payload.bot_id,
            kind=payload.kind,
            reason=payload.reason,
            owner_id=user["id"],
        )

    except RoomError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None


@router.get("/rooms/{room_id}/hands")
def list_hands(
    room_id: int,
    status: str | None = None,
    user: dict = Depends(get_current_user),
):
    try:
        hands = _open_store().list_hands(
            room_id=room_id,
            owner_id=user["id"],
            status=status,
        )

    except RoomError:
        raise _not_found() from None

    return {"hands": hands}


@router.post("/hands/{hand_id}/approve")
def approve_hand(
    hand_id: int,
    user: dict = Depends(get_current_user),
):
    try:
        return approve_and_expand(
            room_store=_open_store(),
            hand_id=hand_id,
            owner_id=user["id"],
        )

    except RoomError:
        raise _not_found() from None


@router.post("/hands/{hand_id}/reject")
def reject_hand(
    hand_id: int,
    payload: HandReject,
    user: dict = Depends(get_current_user),
):
    try:
        return _open_store().reject_hand(
            hand_id=hand_id,
            owner_id=user["id"],
            unfair=payload.unfair,
        )

    except RoomError:
        raise _not_found() from None


@router.post("/rooms/{room_id}/turns/{turn_id}/summary")
def summarize_turn(
    room_id: int,
    turn_id: str,
    user: dict = Depends(get_current_user),
):
    try:
        return host_summarize_turn(
            room_store=_open_store(),
            room_id=room_id,
            owner_id=user["id"],
            turn_id=turn_id,
        )

    except RoomError:
        raise _not_found() from None


@router.post("/rooms/{room_id}/turns/{turn_id}/decision")
def make_decision(
    room_id: int,
    turn_id: str,
    payload: Decision,
    user: dict = Depends(get_current_user),
):
    """用户选择一个决策选项，由默认 Bot 执行工具（S6 收口）。"""

    from src import agent
    from src.core.bot_store import BotStore
    from src.core.reminder_identity import bind_authenticated_reminder

    store = _open_store()

    try:
        messages = store.list_messages(
            room_id=room_id,
            owner_id=user["id"],
            turn_id=turn_id,
        )

    except RoomError:
        raise _not_found() from None

    summary_msg = next(
        (
            message
            for message in reversed(messages)
            if message["role"] == "summary"
        ),
        None,
    )

    if summary_msg is None:
        raise HTTPException(
            status_code=409,
            detail="还没有汇总，请先触发 summary",
        )

    summary = json.loads(summary_msg["content"])
    options = summary.get("options", [])

    chosen = next(
        (
            option
            for option in options
            if option.get("label") == payload.option
        ),
        None,
    )

    if chosen is None:
        raise HTTPException(
            status_code=422,
            detail="选项不存在",
        )

    default_bot = BotStore(store.db_path).get_default_bot(user["id"])

    if default_bot is None:
        raise HTTPException(
            status_code=409,
            detail="没有可用的默认 Bot",
        )

    instruction = (
        f"用户选择了这个决策：{chosen['label']}。"
        f"代价/风险：{chosen.get('risk', '')}。"
        f"请执行这个决策。"
    )

    request_id = uuid.uuid4().hex

    with bind_authenticated_reminder(
        user_id=user["id"],
        request_id=request_id,
        role=user["role"],
    ):
        reply = agent.chat(instruction, bot=default_bot)

    store.add_message(
        room_id=room_id,
        turn_id=turn_id,
        speaker="bot",
        bot_id=default_bot["id"],
        role="decision",
        content=reply,
        owner_id=user["id"],
    )

    # 活跃值结算：站对边的不扣，其余扣 penalty。
    # 选项 label 里含哪个立场名，就算选了哪一边；对不上则全场一起扣。
    room = store.get_room(
        room_id=room_id, owner_id=user["id"]
    ) or {}

    chosen_stance = None

    for stance in room.get("stances") or []:
        if stance and stance in chosen["label"]:
            chosen_stance = stance
            break

    penalized = store.apply_stance_penalty(
        room_id=room_id,
        owner_id=user["id"],
        chosen_stance=chosen_stance,
        penalty=payload.penalty,
    )

    return {
        "decision": chosen["label"],
        "reply": reply,
        "chosen_stance": chosen_stance,
        "penalized": penalized,
    }
