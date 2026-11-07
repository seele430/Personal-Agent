"""产物（W4）：登记、路径安全、四类预览。"""

from src.artifacts.models import (
    KINDS,
    Artifact,
    ArtifactError,
    ArtifactForbidden,
    ArtifactNotFound,
    MAX_ARTIFACT_BYTES,
    classify,
    guess_media_type,
)
from src.artifacts.preview import preview_payload
from src.artifacts.registry import (
    ArtifactRegistry,
    get_registry,
    reset_registry,
)


__all__ = [
    "KINDS",
    "Artifact",
    "ArtifactError",
    "ArtifactForbidden",
    "ArtifactNotFound",
    "ArtifactRegistry",
    "MAX_ARTIFACT_BYTES",
    "classify",
    "get_registry",
    "guess_media_type",
    "preview_payload",
    "reset_registry",
]
