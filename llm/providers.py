"""OpenAI 兼容 Provider 实现(指导书 5.1/5.2/5.4)。

实现要点:
- 直接用 httpx 调用 {base_url}/chat/completions,不引入 OpenAI SDK,
  避免 SDK 自动重试与业务重试叠加(5.4);
- HTTP 状态分类:429/5xx/网络错误 -> retryable;400/401/403/404 等 -> fatal;
- 绝不把 api_key 写进异常、日志或 LLMResponse.raw_metadata;
- raw_metadata 只保留白名单字段。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .base import LLMFatalError, LLMProvider, LLMRetryableError
from .schemas import LLMResponse

# raw_metadata 白名单:只保留这些响应字段(均不含密钥)
_METADATA_ALLOWLIST = {"id", "object", "created", "system_fingerprint", "service_tier"}

_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _classify_status(status: int) -> str:
    return "retryable" if status in _RETRYABLE_STATUS else "fatal"


class OpenAICompatibleProvider(LLMProvider):
    """通过 OpenAI 兼容 Chat Completions API 接入的 Provider。"""

    def __init__(
        self,
        name: str,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 45.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise LLMFatalError(f"provider {name!r} 缺少 base_url")
        if not api_key:
            raise LLMFatalError(f"provider {name!r} 缺少 api_key")
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    # repr/str 默认会包含 _api_key 引用,显式覆盖防止意外泄漏
    def __repr__(self) -> str:
        return f"OpenAICompatibleProvider(name={self.name!r}, base_url={self._base_url!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

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
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        started = time.perf_counter()
        try:
            resp = await self._client.post(
                "/chat/completions", json=payload, timeout=timeout
            )
        except httpx.TimeoutException as e:
            raise LLMRetryableError(
                f"provider {self.name!r} model {model!r} 请求超时",
                provider=self.name,
                model=model,
            ) from e
        except httpx.HTTPError as e:
            # httpx 异常信息通常不含请求头;仍不透传原始异常细节到上层消息
            raise LLMRetryableError(
                f"provider {self.name!r} model {model!r} 网络错误: {type(e).__name__}",
                provider=self.name,
                model=model,
            ) from e

        latency_ms = (time.perf_counter() - started) * 1000.0

        if resp.status_code != 200:
            kind = _classify_status(resp.status_code)
            msg = (
                f"provider {self.name!r} model {model!r} "
                f"HTTP {resp.status_code}(响应体已忽略,不含密钥)"
            )
            if kind == "retryable":
                raise LLMRetryableError(msg, provider=self.name, model=model)
            raise LLMFatalError(msg, provider=self.name, model=model)

        try:
            data = resp.json()
        except ValueError as e:
            raise LLMFatalError(
                f"provider {self.name!r} model {model!r} 返回非 JSON 响应体",
                provider=self.name,
                model=model,
            ) from e

        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMFatalError(
                f"provider {self.name!r} model {model!r} 响应结构不符(缺 choices/message)",
                provider=self.name,
                model=model,
            ) from e

        if content is None:
            content = ""

        usage = data.get("usage") or {}
        request_id = data.get("id") or resp.headers.get("x-request-id")
        metadata = {
            k: v for k, v in data.items() if k in _METADATA_ALLOWLIST and k != "id"
        }

        return LLMResponse(
            content=content,
            provider=self.name,
            model=data.get("model", model),
            model_alias="",  # 由 Router 填入
            request_id=request_id,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            latency_ms=latency_ms,
            finish_reason=choice.get("finish_reason"),
            raw_metadata=metadata,
        )
