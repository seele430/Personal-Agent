"""外部 Provider 适配器。

当前只有一个实现：OpenAI 兼容的模型端点。

**它不是"外部 Agent"**：没有任务状态机、没有工具、没有外部产物目录。
它提供的是真实网络、真实鉴权错误与真实 token 计量——用来把"纯 mock"升级为"真联网"。
"""

from src.collab.providers.openai_compat import OpenAICompatProvider, ProviderError

__all__ = ["OpenAICompatProvider", "ProviderError"]
