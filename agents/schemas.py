"""Agent 输出 Schema(阶段 3,指导书 6.2)。

约束:
- 全部 extra="forbid":未知字段静默进入执行层是禁止的;
- 金额用 Decimal,不用 float 做资金计算;
- action / strategy 用 Literal/Enum;
- confidence 仅为模型自述元数据,不可映射为成功概率或仓位倍数。
"""

from __future__ import annotations

import enum
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class Sentiment(str, enum.Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    SIDEWAYS = "sideways"
    UNCERTAIN = "uncertain"


class Action(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class StrategyName(str, enum.Enum):
    DCA = "DCA"
    TREND_FOLLOWING = "TREND_FOLLOWING"
    NO_TRADE = "NO_TRADE"
    GRID = "GRID"  # 仅研究,决策层不得选它(见 TradingDecision 校验)


class MarketAnalysis(BaseModel):
    """Market Agent 输出。只描述市场,不含订单/仓位决策。"""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    sentiment: Sentiment
    confidence: float = Field(ge=0.0, le=1.0)
    trend_summary: str = Field(min_length=1, max_length=2000)
    volatility_summary: str = Field(min_length=1, max_length=1000)
    key_observations: list[str] = Field(default_factory=list, max_length=20)
    data_limitations: list[str] = Field(default_factory=list, max_length=20)


class StrategyProposal(BaseModel):
    """Strategy Agent 输出。提出方案与触发/失效条件,不下单。"""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    strategy: StrategyName
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=2000)
    trigger_conditions: list[str] = Field(default_factory=list, max_length=20)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=20)


class TradingDecision(BaseModel):
    """Decision Agent 输出的结构化交易计划(指导书 6.2 字段清单)。"""

    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=8, max_length=64)
    symbol: str
    action: Action
    strategy: StrategyName
    amount_usdt: Decimal | None = None  # BUY/SELL 必须给;HOLD 必须为 None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=3000)
    created_at: int  # UTC ms
    expires_at: int  # UTC ms,信号有效期

    @field_validator("expires_at")
    @classmethod
    def _expires_after_created(cls, v: int, info: ValidationInfo) -> int:
        created = info.data.get("created_at")
        if created is not None and v <= created:
            raise ValueError("expires_at 必须晚于 created_at")
        return v

    @field_validator("amount_usdt")
    @classmethod
    def _amount_rules(cls, v: Decimal | None, info: ValidationInfo) -> Decimal | None:
        action = info.data.get("action")
        if action in (Action.BUY, Action.SELL):
            if v is None or v <= 0:
                raise ValueError("BUY/SELL 必须提供正的 amount_usdt")
        if action == Action.HOLD and v is not None:
            raise ValueError("HOLD 不允许携带 amount_usdt")
        return v

    @field_validator("strategy")
    @classmethod
    def _no_grid_in_decisions(cls, v: StrategyName) -> StrategyName:
        if v == StrategyName.GRID:
            raise ValueError("GRID 仅研究,不得作为决策策略")
        return v


class AgentRunMeta(BaseModel):
    """单个 Agent 节点的执行元数据(可追踪性)。"""

    model_config = ConfigDict(extra="forbid")

    agent: str
    task: str
    model_alias: str | None = None
    status: str  # ok | skipped_data_error | llm_failed | parse_failed
    note: str = ""
