"""外部 Provider 适配器。

三种来源：

- **Qwen Code**（本机独立 agent 进程）：`PA_PROVIDER=qwen`；
- **火山方舟（豆包）**：`ARK_API_KEY` 非空时优先；
- **OpenAI 兼容端点**（默认 DeepSeek）：`OPENAI_API_KEY`。

注意：只有 Qwen Code 是"外部 Agent"（自己的循环与工具），另外两种都是"模型 API"。
"""

from __future__ import annotations

import os

from src.collab.providers.openai_compat import OpenAICompatProvider, ProviderError
from src.collab.providers.qwen_agent import QwenAgentProvider


ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
ARK_DEFAULT_MODEL = "doubao-seed-2-0-mini-260428"


def build_provider():
    """按环境变量选一个 provider；显式 PA_PROVIDER=qwen 优先，其次 ARK。"""

    if (os.getenv("PA_PROVIDER") or "").strip().lower() == "qwen":
        return QwenAgentProvider()

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


def build_providers(chain: list[str]) -> dict:
    """按名字构造 Provider 映射（顺序即接力顺序）。"""

    providers = {}

    for name in chain:
        key = (name or "").strip().lower()

        if key == "qwen":
            providers[key] = QwenAgentProvider()
        elif key == "ark":
            provider = OpenAICompatProvider(
                api_key=(os.getenv("ARK_API_KEY") or "").strip(),
                base_url=(os.getenv("ARK_BASE_URL") or ARK_BASE_URL).strip(),
                model=(os.getenv("ARK_MODEL") or os.getenv("MODEL") or ARK_DEFAULT_MODEL).strip(),
            )
            provider.name = "ark"
            providers[key] = provider
        elif key in ("deepseek", "openai", "openai-compat"):
            provider = OpenAICompatProvider()
            provider.name = key
            providers[key] = provider

    return providers


def provider_name() -> str:
    if (os.getenv("PA_PROVIDER") or "").strip().lower() == "qwen":
        return "qwen-agent"

    return "ark" if (os.getenv("ARK_API_KEY") or "").strip() else "openai-compat"


__all__ = [
    "ARK_BASE_URL",
    "ARK_DEFAULT_MODEL",
    "OpenAICompatProvider",
    "ProviderError",
    "QwenAgentProvider",
    "build_provider",
    "build_providers",
    "provider_name",
]
