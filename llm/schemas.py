"""LLM 层 Pydantic Schema(指导书 5.1/5.2)。

约束:
- LLMResponse 记录一次模型调用的完整元数据,不含任何密钥;
- raw_metadata 必须经过脱敏后才能写入;
- 金额/价格不在此层出现(那是决策层 Decimal 的职责)。
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskType(str, enum.Enum):
    """模型任务类型(指导书 5.3 路由表)。"""

    MARKET_SUMMARY = "market_summary"
    MARKET_ANALYSIS = "market_analysis"
    STRATEGY_ANALYSIS = "strategy_analysis"
    DECISION = "decision"
    ANOMALY_ANALYSIS = "anomaly_analysis"
    RISK_EXPLANATION = "risk_explanation"


class LLMResponse(BaseModel):
    """一次成功模型调用的结果(指导书 5.2)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    provider: str
    model: str
    model_alias: str
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float
    finish_reason: str | None = None
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class SafeAction(str, enum.Enum):
    """模型链路失败时唯一允许的动作。"""

    HOLD = "HOLD"


SAFE_ON_FAILURE = SafeAction.HOLD


class SafeFailure(BaseModel):
    """全部模型不可用时返回给工作流的安全结构。

    工作流必须把它映射为 HOLD / NO_TRADE,不得据此产生任何交易指令。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str
    action: SafeAction = SafeAction.HOLD
    reason: str
    attempts: int = 0
