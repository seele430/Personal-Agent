"""解析本机监听端口，判断是否暴露到非回环地址。

只做静态解析，方便测试；真正的 netstat 调用在 check_listeners.py。
"""

from __future__ import annotations

import re

from dataclasses import dataclass

_LISTEN_RE = re.compile(
    r"^\s*(?P<proto>TCP|UDP)\s+"
    r"(?P<local>\S+)\s+"
    r"(?P<remote>\S+)\s+"
    r"(?P<state>LISTENING|LISTEN)?\s*"
    r"(?P<pid>\d+)?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Listener:
    proto: str
    address: str
    port: int
    pid: str
    state: str


def is_loopback(address: str) -> bool:
    address = address.strip("[]").lower()

    if address in {"::1", "localhost"}:
        return True

    return address.startswith("127.")


def parse_netstat(text: str) -> list[Listener]:
    listeners: list[Listener] = []

    for line in text.splitlines():
        match = _LISTEN_RE.match(line)

        if not match:
            continue

        local = match.group("local")
        state = (match.group("state") or "").upper()

        if state != "LISTENING":
            continue

        if ":" not in local:
            continue

        address, _, port = local.rpartition(":")

        if not port.isdigit():
            continue

        listeners.append(
            Listener(
                proto=match.group("proto").upper(),
                address=address,
                port=int(port),
                pid=(match.group("pid") or "").strip(),
                state=state,
            )
        )

    return listeners


def exposed(listeners: list[Listener], port: int | None = None) -> list[Listener]:
    """返回绑定在非回环地址上的监听项。"""

    result = []

    for item in listeners:
        if port is not None and item.port != port:
            continue

        if is_loopback(item.address):
            continue

        result.append(item)

    return result
