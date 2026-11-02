"""阶段 1:OpenAI 兼容 Provider 测试(指导书 14.1,httpx.MockTransport 离线)。"""

from __future__ import annotations

import json

import httpx
import pytest

from llm.base import LLMFatalError, LLMRetryableError
from llm.providers import OpenAICompatibleProvider

KEY = "sk-super-secret-key-xyz"


def _provider(handler) -> OpenAICompatibleProvider:
    transport = httpx.MockTransport(handler)
    return OpenAICompatibleProvider(
        "primary",
        base_url="https://unit.example/v1",
        api_key=KEY,
        timeout=5,
        transport=transport,
    )


def _ok_body(content: str = '{"x": 1}') -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-123",
            "model": "fast-model",
            "choices": [
                {"message": {"content": content}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    ).encode()


async def test_success_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        return httpx.Response(200, content=_ok_body())

    p = _provider(handler)
    try:
        resp = await p.generate(
            model="fast-model",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=100,
        )
    finally:
        await p.aclose()
    assert resp.content == '{"x": 1}'
    assert resp.provider == "primary"
    assert resp.input_tokens == 10
    assert resp.output_tokens == 5
    assert resp.request_id == "chatcmpl-123"
    assert resp.latency_ms >= 0


async def test_timeout_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("boom")

    p = _provider(handler)
    try:
        with pytest.raises(LLMRetryableError) as ei:
            await p.generate(
                model="m", messages=[], temperature=0.1, max_tokens=10
            )
        assert ei.value.retryable
        assert KEY not in str(ei.value)
    finally:
        await p.aclose()


async def test_429_retryable():
    p = _provider(lambda r: httpx.Response(429))
    try:
        with pytest.raises(LLMRetryableError):
            await p.generate(model="m", messages=[], temperature=0.1, max_tokens=10)
    finally:
        await p.aclose()


async def test_5xx_retryable():
    p = _provider(lambda r: httpx.Response(503))
    try:
        with pytest.raises(LLMRetryableError):
            await p.generate(model="m", messages=[], temperature=0.1, max_tokens=10)
    finally:
        await p.aclose()


async def test_401_fatal():
    p = _provider(lambda r: httpx.Response(401))
    try:
        with pytest.raises(LLMFatalError) as ei:
            await p.generate(model="m", messages=[], temperature=0.1, max_tokens=10)
        assert not ei.value.retryable
        assert KEY not in str(ei.value)
    finally:
        await p.aclose()


async def test_400_fatal():
    p = _provider(lambda r: httpx.Response(400))
    try:
        with pytest.raises(LLMFatalError):
            await p.generate(model="m", messages=[], temperature=0.1, max_tokens=10)
    finally:
        await p.aclose()


async def test_bad_structure_fatal():
    p = _provider(lambda r: httpx.Response(200, content=b'{"nope": true}'))
    try:
        with pytest.raises(LLMFatalError):
            await p.generate(model="m", messages=[], temperature=0.1, max_tokens=10)
    finally:
        await p.aclose()


def test_missing_key_fatal():
    with pytest.raises(LLMFatalError):
        OpenAICompatibleProvider("p", base_url="https://x/v1", api_key="")


def test_repr_hides_key():
    p = _provider(lambda r: httpx.Response(200, content=_ok_body()))
    assert KEY not in repr(p)
    assert KEY not in str(p)
