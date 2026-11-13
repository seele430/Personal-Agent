"""小号上线检测：OneBot 在线状态从「离线」变「在线」时开一个新会话。

为什么需要：NapCat 的 HTTP 客户端没有推送 `meta_event.lifecycle`（实测 0 条），
所以「每次拉起小号 = 新对话」只能靠轮询 `get_status` 的状态翻转来判定。

边界：
- 只在 False → True 的翻转时建会话；首次探测（last=None）只记录基线，不建；
- 轮询失败（OneBot 不在线）视为离线，不抛异常；
- 线程是 daemon，随 API 进程退出。
"""

from __future__ import annotations

import os
import threading
import time

from src.qq.onebot import OneBotClient
from src.sessions import SessionStore


DEFAULT_INTERVAL = 30.0
_started = False


def build_client() -> OneBotClient:
    return OneBotClient(
        base_url=os.getenv("PA_ONEBOT_URL") or "",
        token=os.getenv("PA_ONEBOT_TOKEN") or "",
    )


def is_online(client) -> bool:
    try:
        data = client._call("get_status")
    except Exception:  # noqa: BLE001 - 任何失败都按离线处理
        return False

    # OneBotClient._call 已经把 body["data"] 拆出来了
    return bool((data or {}).get("online"))


def poll_once(
    state: dict,
    *,
    client,
    owner: int,
    store_factory=SessionStore,
    clock=time.time,
) -> dict:
    online = is_online(client)

    if state.get("last") is False and online and int(owner) > 0:
        try:
            title = "QQ · " + time.strftime("%H:%M", time.localtime(clock()))
            store_factory().ensure_qq_launch(
                str(int(clock())), owner_id=int(owner), title=title
            )
        except Exception:  # noqa: BLE001 - 建会话失败不影响检测循环
            pass

    state["last"] = online

    return state


def watch_forever(interval: float = DEFAULT_INTERVAL, *, client=None, owner: int = 0) -> None:
    client = client or build_client()
    state: dict = {}
    owner = int(owner or os.getenv("PA_QQ_OWNER") or 0)

    while True:
        poll_once(state, client=client, owner=owner)
        time.sleep(max(5.0, float(interval)))


def start(interval: float = DEFAULT_INTERVAL) -> bool:
    """启动一次（重复调用无副作用）；返回是否真的启动了。"""

    global _started

    if _started:
        return False

    if (os.getenv("PA_QQ_ENABLED") or "").strip() != "1":
        return False

    if not (os.getenv("PA_ONEBOT_URL") or "").strip():
        return False

    if (os.getenv("PA_QQ_LAUNCH_WATCH") or "1").strip() == "0":
        return False

    _started = True
    threading.Thread(target=watch_forever, args=(interval,), daemon=True).start()

    return True
