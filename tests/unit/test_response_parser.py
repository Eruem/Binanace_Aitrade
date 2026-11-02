"""阶段 1:LLM 输出解析测试(指导书 14.2/14.3)。"""

import pytest
from pydantic import BaseModel

from llm.base import LLMResponseParseError
from llm.response_parser import parse_llm_json


class Simple(BaseModel):
    action: str
    confidence: float


def test_valid_json():
    out = parse_llm_json('{"action": "HOLD", "confidence": 0.5}', Simple)
    assert out.action == "HOLD"


def test_fenced_json_accepted():
    out = parse_llm_json('```json\n{"action": "BUY", "confidence": 0.7}\n```', Simple)
    assert out.action == "BUY"


def test_invalid_json_rejected():
    with pytest.raises(LLMResponseParseError):
        parse_llm_json("这不是 JSON", Simple)


def test_empty_rejected():
    with pytest.raises(LLMResponseParseError):
        parse_llm_json("", Simple)
    with pytest.raises(LLMResponseParseError):
        parse_llm_json(None, Simple)


def test_non_object_rejected():
    with pytest.raises(LLMResponseParseError):
        parse_llm_json("[1, 2, 3]", Simple)


def test_unknown_field_rejected():
    with pytest.raises(LLMResponseParseError):
        parse_llm_json(
            '{"action": "BUY", "confidence": 0.9, "evil_extra": true}', Simple
        )


def test_missing_field_rejected():
    with pytest.raises(LLMResponseParseError):
        parse_llm_json('{"action": "BUY"}', Simple)


def test_wrong_type_rejected():
    with pytest.raises(LLMResponseParseError):
        parse_llm_json('{"action": 123, "confidence": "high"}', Simple)
