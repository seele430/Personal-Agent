"""外部 Provider 适配器。

两种来源，同一套 OpenAI 兼容客户端：

- **火山方舟（豆包）**：`ARK_API_KEY` 非空时优先；
- **OpenAI 兼容端点**（默认 DeepSeek）：`OPENAI_API_KEY`。

注意：两者都是"**模型 API**"，不是"外部 Agent"。
"""

from __future__ import annotations

import os

from src.collab.providers.openai_compat import OpenAICompatProvider, ProviderError


ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
ARK_DEFAULT_MODEL = "doubao-seed-2-0-mini-260428"


def build_provider() -> OpenAICompatProvider:
    """按环境变量选一个 provider；ARK 优先。"""

    ark_key = (os.getenv("ARK_API_KEY") or "").strip()

    if ark_key:
        provider = OpenAICompatProvider(
            api_key=ark_key,
            base_url=(os.getenv("ARK_BASE_URL") or ARK_BASE_URL).strip(),
            model=(os.getenv("ARK_MODEL") or os.getenv("MODEL") or ARK_DEFAULT_MODEL).strip(),
        )
        provider.name = "ark"

        return provider

    return OpenAICompatProvider()


def provider_name() -> str:
    return "ark" if (os.getenv("ARK_API_KEY") or "").strip() else "openai-compat"


__all__ = [
    "ARK_BASE_URL",
    "ARK_DEFAULT_MODEL",
    "OpenAICompatProvider",
    "ProviderError",
    "build_provider",
    "provider_name",
]
