"""记忆条目的领域模型：类型、分层、信任等级、时间窗。

与 `src/knowledge/` 的区别（这是 W3 的核心区分）：

- **knowledge** 管"文档与片段"——一份资料被切成 chunk，用于检索原文；
- **memory** 管"事实与偏好"——一句话就是一条事实，有生效/失效时间，
  同一件事改主意会产生**新版本**而不是覆盖旧记录。

所以这里不存 chunk，只存条目；不存正文段落，只存可判定的 key/value/text。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


#: 记忆类型。`conflict` 不是类型，而是 `conflict_group` 上的关系。
KINDS = ("preference", "project_fact", "constraint", "episode")

#: 语义分层：稳定身份偏好 / 项目事实 / 会话临时。
LAYERS = ("identity", "project", "session")

#: 四级信任，从低到高（§8.1）。顺序即等级。
TRUST_LEVELS = (
    "model_inferred",
    "external_untrusted",
    "platform_verified",
    "user_confirmed",
)

TRUST_RANK = {name: index for index, name in enumerate(TRUST_LEVELS)}

MAX_TEXT_CHARS = 4000
MAX_KEY_CHARS = 200
MAX_VALUE_CHARS = 2000
MAX_GROUP_CHARS = 200
MAX_REF_CHARS = 500


class MemoryError(ValueError):
    """记忆条目不合法。"""


def utc_now() -> str:
    """统一的时间口径：UTC ISO 字符串，字典序即时间序。"""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_time(value: str | None, *, field: str) -> str | None:
    """把时间串收敛成统一格式；空值返回 None。"""

    if value is None:
        return None

    if not isinstance(value, str) or not value.strip():
        raise MemoryError(f"{field} 必须是非空字符串或 None")

    text = value.strip()

    try:
        parsed = datetime.fromisoformat(text)

    except ValueError as error:
        raise MemoryError(f"{field} 不是合法 ISO 时间：{text}") from error

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def trust_rank(trust: str) -> int:
    if trust not in TRUST_RANK:
        raise MemoryError(f"未知信任等级：{trust!r}")

    return TRUST_RANK[trust]


def trusted_at_least(min_trust: str) -> tuple[str, ...]:
    """返回等级 ≥ `min_trust` 的全部取值（用于 SQL IN）。"""

    rank = trust_rank(min_trust)

    return tuple(name for name, value in TRUST_RANK.items() if value >= rank)


def validate_owner(owner_id) -> str:
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise MemoryError("owner_id 不能为空")

    return owner_id.strip()


def validate_kind(kind) -> str:
    if kind not in KINDS:
        raise MemoryError(f"未知记忆类型：{kind!r}，可选 {KINDS}")

    return kind


def validate_layer(layer) -> str:
    if layer not in LAYERS:
        raise MemoryError(f"未知记忆分层：{layer!r}，可选 {LAYERS}")

    return layer


def validate_trust(trust) -> str:
    if trust not in TRUST_RANK:
        raise MemoryError(f"未知信任等级：{trust!r}，可选 {TRUST_LEVELS}")

    return trust


def validate_text(text) -> str:
    if not isinstance(text, str) or not text.strip():
        raise MemoryError("记忆正文不能为空")

    cleaned = " ".join(text.split())

    if len(cleaned) > MAX_TEXT_CHARS:
        raise MemoryError(f"记忆正文超过 {MAX_TEXT_CHARS} 字符")

    return cleaned


def validate_short(value, *, field: str, limit: int, required=False) -> str:
    if value is None:
        if required:
            raise MemoryError(f"{field} 不能为空")

        return ""

    if not isinstance(value, str):
        raise MemoryError(f"{field} 必须是字符串")

    cleaned = " ".join(value.split())

    if required and not cleaned:
        raise MemoryError(f"{field} 不能为空")

    if len(cleaned) > limit:
        raise MemoryError(f"{field} 超过 {limit} 字符")

    return cleaned


@dataclass(frozen=True)
class MemoryItem:
    id: int
    owner_id: str
    kind: str
    layer: str
    trust: str
    key: str
    value: str
    text: str
    source_ref: str
    source_doc_id: int | None
    valid_from: str
    valid_to: str | None
    supersedes_id: int | None
    conflict_group: str
    created_at: str
    updated_at: str
    deleted_at: str | None = None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def is_effective(self, at: str | None = None) -> bool:
        """在给定时刻是否生效（左闭右开）。"""

        moment = at or utc_now()

        if self.is_deleted:
            return False

        if self.valid_from > moment:
            return False

        if self.valid_to is not None and self.valid_to <= moment:
            return False

        return True


@dataclass(frozen=True)
class MemoryHit:
    item: MemoryItem
    score: float
    rank: int
    mode: str


@dataclass(frozen=True)
class MemoryEvent:
    id: int
    item_id: int
    action: str
    actor: str
    reason: str
    at: str
