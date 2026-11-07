"""访问日志：每次远程请求一条 JSONL，令牌/凭据一律脱敏。

脱敏是**白名单式**的：只记录方法、路径（query 已剔除）、状态码、耗时、客户端地址、
User-Agent 摘要。任何 header 都不整条落盘，避免"顺手记了 Authorization"。
"""

from __future__ import annotations

import json
import re

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path


SENSITIVE = re.compile(r"(token|authorization|cookie|secret|password)", re.IGNORECASE)
TOKEN_QUERY = re.compile(r"([?&](?:token|access_token)=)([^&]*)", re.IGNORECASE)


class AccessLogError(RuntimeError):
    """日志写入失败。"""


def strip_query(path: str) -> str:
    """只保留 path，扔掉 query（query 里常有令牌）。"""

    return (path or "/").split("?", 1)[0]


def redact(text: str) -> str:
    """把 query 里的令牌替换成 ***。"""

    return TOKEN_QUERY.sub(lambda match: f"{match.group(1)}***", text or "")


def redact_headers(headers) -> dict:
    """只保留非敏感 header，敏感值替换成 ***。"""

    result = {}

    for key, value in headers or []:
        name = key.decode() if isinstance(key, bytes) else str(key)

        if SENSITIVE.search(name):
            result[name.lower()] = "***"

        else:
            result[name.lower()] = (
                value.decode() if isinstance(value, bytes) else str(value)
            )

    return result


@dataclass(frozen=True)
class AccessRecord:
    ts: str
    method: str
    path: str
    status: int
    duration_ms: float
    client: str
    user_agent: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class AccessLog:
    """JSONL 追加写入；目录不存在会自动创建。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, record: AccessRecord) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)

            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

        except OSError as error:
            raise AccessLogError(f"写访问日志失败：{error}") from error

    def read(self) -> list[dict]:
        if not self.path.is_file():
            return []

        records = []

        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()

            if not line:
                continue

            records.append(json.loads(line))

        return records


def build_record(
    *,
    method: str,
    path: str,
    status: int,
    duration_ms: float,
    client: str,
    user_agent: str = "",
    note: str = "",
) -> AccessRecord:
    return AccessRecord(
        ts=datetime.now(timezone.utc).isoformat(),
        method=(method or "").upper(),
        path=redact(strip_query(path)),
        status=int(status),
        duration_ms=round(float(duration_ms), 2),
        client=client or "",
        user_agent=(user_agent or "")[:120],
        note=note,
    )
