"""LLM 基础接口与错误分类(指导书 5.2/5.4)。

错误分类原则(5.4):
- retryable:网络超时、限流(429)、服务端 5xx —— 允许有限重试;
- fatal:参数错误、鉴权失败、响应结构错误 —— 快速失败并记录。

任何异常的字符串表示都不得包含 API Key。Provider 层负责保证这一点。
"""

from __future__ import annotations

import abc
from typing import Any

from .schemas import LLMResponse


class LLMError(Exception):
    """LLM 层基础异常。"""

    retryable: bool = False

    def __init__(self, message: str, *, provider: str = "", model: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model


class LLMRetryableError(LLMError):
    """可重试:超时、限流、5xx、连接错误。"""

    retryable = True


class LLMFatalError(LLMError):
    """不可重试:鉴权、参数、协议错误。"""

    retryable = False


class LLMResponseParseError(LLMFatalError):
    """模型返回内容无法通过业务 Schema 校验(5.4:响应结构错误不可重试)。"""


class AllModelsUnavailableError(LLMError):
    """主模型与备用模型均失败。工作层必须转为 HOLD / NO_TRADE。"""

    retryable = False


class CycleLimitExceededError(LLMFatalError):
    """超过每轮最大模型调用次数。"""


class LLMProvider(abc.ABC):
    """所有 Provider 的统一异步接口(指导书 5.2)。

    Agent 不得直接实例化任何 SDK 客户端;只能经 ModelRouter -> 这里。
    """

    name: str = "abstract"

    @abc.abstractmethod
    async def generate(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        timeout: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """执行一次 Chat Completions 调用。失败抛 LLMError 子类。"""

    async def aclose(self) -> None:
        """释放底层资源(可选实现)。"""
