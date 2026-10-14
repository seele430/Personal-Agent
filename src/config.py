"""Personal Agent 集中配置。

所有数据库路径、日志路径统一在此定义。
测试时替换 paths 对象，所有模块自动生效，避免导入绑定陷阱。
"""

from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Paths:
    """项目路径配置。frozen=True 防止运行时意外修改。"""

    # 数据库
    tasks_db: Path
    reminders_db: Path
    chat_requests_db: Path
    auth_users_db: Path
    ai_usage_db: Path

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


# 全局路径配置实例。测试通过 patch.object(config, "paths", ...) 替换。
paths = Paths(
    tasks_db=BASE_DIR / "tasks.db",
    reminders_db=BASE_DIR / "reminders.db",
    chat_requests_db=BASE_DIR / "chat_requests.db",
    auth_users_db=BASE_DIR / "auth_users.db",
    ai_usage_db=BASE_DIR / "ai_usage.db",
)