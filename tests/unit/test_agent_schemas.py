"""阶段 3:Agent 输出 Schema 测试(指导书 6.2)。"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from agents.schemas import (
    Action,
    MarketAnalysis,
    Sentiment,
    StrategyName,
    StrategyProposal,
    TradingDecision,
)


def _decision_kwargs(**over):
    base = dict(
        decision_id="cycle-x-1",
        symbol="BTC/USDT",
        action=Action.HOLD,
        strategy=StrategyName.NO_TRADE,
        amount_usdt=None,
        confidence=0.5,
        reason="test",
        created_at=1_793_145_600_000,
        expires_at=1_793_149_200_000,
    )
    base.update(over)
    return base


class TestTradingDecision:
    def test_hold_ok(self):
        d = TradingDecision(**_decision_kwargs())
        assert d.action == Action.HOLD

    def test_buy_requires_positive_amount(self):
        with pytest.raises(ValidationError):
            TradingDecision(
                **_decision_kwargs(
                    action=Action.BUY,
                    strategy=StrategyName.DCA,
                    amount_usdt=None,
                )
            )
        with pytest.raises(ValidationError):
            TradingDecision(
                **_decision_kwargs(
                    action=Action.BUY,
                    strategy=StrategyName.DCA,
                    amount_usdt=Decimal("-1"),
                )
            )

    def test_buy_with_amount_ok(self):
        d = TradingDecision(
            **_decision_kwargs(
                action=Action.BUY,
                strategy=StrategyName.DCA,
                amount_usdt=Decimal("10.5"),
            )
        )
        assert d.amount_usdt == Decimal("10.5")

    def test_hold_rejects_amount(self):
        with pytest.raises(ValidationError):
            TradingDecision(
                **_decision_kwargs(amount_usdt=Decimal("5"))
            )

    def test_grid_rejected(self):
        with pytest.raises(ValidationError):
            TradingDecision(**_decision_kwargs(strategy=StrategyName.GRID))

    def test_expires_must_be_after_created(self):
        with pytest.raises(ValidationError):
            TradingDecision(
                **_decision_kwargs(expires_at=1_793_145_600_000)
            )

    def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError):
            TradingDecision(**_decision_kwargs(leverage=3))


class TestAgentOutputs:
    def test_market_analysis_minimal(self):
        m = MarketAnalysis(
            symbol="BTC/USDT",
            sentiment=Sentiment.UNCERTAIN,
            confidence=0.4,
            trend_summary="t",
            volatility_summary="v",
        )
        assert m.sentiment == Sentiment.UNCERTAIN

    def test_market_analysis_bad_sentiment(self):
        with pytest.raises(ValidationError):
            MarketAnalysis(
                symbol="BTC/USDT",
                sentiment="moon",
                confidence=0.4,
                trend_summary="t",
                volatility_summary="v",
            )

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            MarketAnalysis(
                symbol="BTC/USDT",
                sentiment=Sentiment.BULLISH,
                confidence=1.5,
                trend_summary="t",
                volatility_summary="v",
            )

    def test_strategy_proposal_grid_allowed_at_proposal_level(self):
        # 提案层可含 GRID(决策层拒绝),提示词已禁止但 Schema 不拦截提案
        p = StrategyProposal(
            symbol="BTC/USDT",
            strategy=StrategyName.GRID,
            confidence=0.3,
            rationale="research",
        )
        assert p.strategy == StrategyName.GRID
