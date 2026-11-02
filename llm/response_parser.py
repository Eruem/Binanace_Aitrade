"""LLM 输出解析(指导书 5.4 / 6.2)。

原则:
- LLM 输出不是可信指令;必须经 Pydantic Schema 校验;
- 非法 JSON、空内容、未知字段一律拒绝(LLMFatalError,不可重试);
- 兼容模型输出 ```json 代码栅栏,但仅此而已,不做宽松猜测。
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .base import LLMResponseParseError

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text.strip()


def parse_llm_json(content: str | None, schema: type[T]) -> T:
    """把模型输出解析为指定 Pydantic Schema。

    Raises:
        LLMResponseParseError: 空内容 / 非法 JSON / 校验失败(含未知字段)。
    """
    if content is None or not content.strip():
        raise LLMResponseParseError("模型输出为空,拒绝解析")

    candidate = _strip_fences(content)
    try:
        data: Any = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise LLMResponseParseError(
            f"模型输出不是合法 JSON(line {e.lineno}, col {e.colno})"
        ) from e

    if not isinstance(data, dict):
        raise LLMResponseParseError(
            f"模型输出必须是 JSON 对象,收到 {type(data).__name__}"
        )

    # 指导书 6.2:禁止未知字段静默进入执行层。不依赖 schema 的 extra 配置,
    # 解析层直接拒绝任何未声明字段。
    allowed = set(schema.model_fields)
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise LLMResponseParseError(
            f"模型输出包含未知字段: {', '.join(unknown)}"
        )

    try:
        return schema.model_validate(data)
    except ValidationError as e:
        # pydantic 错误信息不含任何外部密钥,可安全透出字段级细节
        raise LLMResponseParseError(f"模型输出未通过 Schema 校验: {e}") from e
