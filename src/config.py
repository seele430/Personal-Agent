"""Personal Agent 集中配置。

所有数据库路径、日志路径统一在此定义。

测试隔离有两条路：
1. 受保护测试运行器会把 PERSONAL_AGENT_DATA_DIR 指向临时目录，
   于是所有数据库（含未显式 patch 的模块）自动落到临时目录；
2. 单个用例想用自己的临时库时，patch 本模块的 paths 对象即可 ——
   但注意 `from src.config import paths` 是早绑定，必须连同
   已导入模块里的 paths 名字一起替换，测试请统一用
   tests.helper.isolate_paths()。
"""

import os

from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

# 数据目录覆盖开关。受保护测试运行器用它把全部数据库隔离到临时目录。
DATA_DIR_ENV = "PERSONAL_AGENT_DATA_DIR"


@dataclass(frozen=True)
class Paths:
    """项目路径配置。frozen=True 防止运行时意外修改。"""

    # 数据库
    tasks_db: Path
    reminders_db: Path
    chat_requests_db: Path
    auth_users_db: Path
    ai_usage_db: Path
    knowledge_db: Path
    bots_db: Path

    # 为兼容性保留的属性别名（逐步迁移后可删除）
    @property
    def CHAT_DB_PATH(self) -> Path:
        return self.chat_requests_db

    @property
    def AUTH_DB_PATH(self) -> Path:
        return self.auth_users_db

    @property
    def USAGE_DB_PATH(self) -> Path:
        return self.ai_usage_db


def build_paths(data_dir: str | Path | None = None) -> Paths:
    """按数据目录构造路径集合。

    优先级：显式参数 > PERSONAL_AGENT_DATA_DIR 环境变量 > 项目根目录。
    """

    if data_dir is None:
        override = os.getenv(DATA_DIR_ENV, "").strip()
        root = Path(override).expanduser() if override else BASE_DIR
    else:
        root = Path(data_dir).expanduser()

    root = root.resolve()

    return Paths(
        tasks_db=root / "tasks.db",
        reminders_db=root / "reminders.db",
        chat_requests_db=root / "chat_requests.db",
        auth_users_db=root / "auth_users.db",
        ai_usage_db=root / "ai_usage.db",
        knowledge_db=root / "knowledge.db",
        bots_db=root / "bots.db",
    )


# 全局路径配置实例。测试通过 patch.object(config, "paths", ...) 替换。
paths = build_paths()
