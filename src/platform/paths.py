"""平台共享路径：沙箱工作区根目录。

exec_service 与 sandbox_service 必须用同一个根，否则「数据库里的沙箱」
和「磁盘上的工作区」会对不上。
"""

from __future__ import annotations

import os
import tempfile

from pathlib import Path

ROOT_ENV = "PERSONAL_AGENT_SANDBOX_ROOT"
DEFAULT_ROOT_NAME = "personal-agent-tier1"


def sandbox_root() -> Path:
    override = os.getenv(ROOT_ENV, "").strip()

    if override:
        return Path(override).expanduser()

    return Path(tempfile.gettempdir()) / DEFAULT_ROOT_NAME
