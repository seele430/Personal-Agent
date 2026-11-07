"""网页研究编排（W2）：ResearchGraph 状态机、报告与引用对账。

对外入口见 `graph.run_graph()` 与 `report.render_report()`；
模型调用在 `llm.py`，提示词契约在 `prompts.py`。
"""

from src.research.llm import (
    LLMCall,
    LLMError,
    LLMInvalidResponse,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
    LLMUsage,
    complete,
    complete_json,
    parse_json_object,
)
from src.research.prompts import (
    PromptContractError,
    validate_analyst,
    validate_critic,
    validate_planner,
    validate_writer,
)


__all__ = [
    "LLMCall",
    "LLMError",
    "LLMInvalidResponse",
    "LLMRateLimited",
    "LLMTimeout",
    "LLMUnavailable",
    "LLMUsage",
    "PromptContractError",
    "complete",
    "complete_json",
    "parse_json_object",
    "validate_analyst",
    "validate_critic",
    "validate_planner",
    "validate_writer",
]
