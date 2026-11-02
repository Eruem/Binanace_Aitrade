"""LLM 包:Provider、Registry、动态 Router、解析与用量跟踪。

对外主入口:
- ModelRouter.from_configset(cfg) 构建路由器;
- router.run(task, messages, cycle_id=...) 调用模型;
- router.run_safe(...) 模型全挂时返回 SafeFailure(HOLD)而不是异常;
- parse_llm_json(content, Schema) 严格校验模型输出。
"""

from .base import (
    AllModelsUnavailableError,
    CycleLimitExceededError,
    LLMError,
    LLMFatalError,
    LLMProvider,
    LLMResponseParseError,
    LLMRetryableError,
)
from .complexity import ComplexityInput, ComplexityScorer
from .fallback import FallbackPolicy
from .providers import OpenAICompatibleProvider
from .registry import ModelRegistry
from .response_parser import parse_llm_json
from .router import ModelRouter
from .schemas import LLMResponse, SafeAction, SafeFailure, TaskType
from .usage_tracker import UsageRecord, UsageTracker

__all__ = [
    "AllModelsUnavailableError",
    "ComplexityInput",
    "ComplexityScorer",
    "CycleLimitExceededError",
    "FallbackPolicy",
    "LLMError",
    "LLMFatalError",
    "LLMProvider",
    "LLMResponse",
    "LLMResponseParseError",
    "LLMRetryableError",
    "ModelRegistry",
    "ModelRouter",
    "OpenAICompatibleProvider",
    "SafeAction",
    "SafeFailure",
    "TaskType",
    "UsageRecord",
    "UsageTracker",
    "parse_llm_json",
]
