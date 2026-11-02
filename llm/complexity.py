"""复杂度评分(指导书 5.3)。

规则路由 + 复杂度评分:复杂度只影响模型选择(如 market_analysis 在
fast/reasoning 之间切换),绝不能影响任何风控阈值。

评分因子(确定性、可解释):
- 多周期分析           +2
- 异常波动             +2
- 数据冲突             +3
- 数据不完整           +2
- Agent 建议冲突       +3
- 多候选策略比较       +1
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ComplexityInput:
    """一次分析任务的复杂度特征。全部由确定性代码填入,不由 LLM 自述。"""

    multi_timeframe: bool = False
    abnormal_volatility: bool = False
    data_conflict: bool = False
    data_incomplete: bool = False
    agent_conflict: bool = False
    multi_strategy_comparison: bool = False
    extra_factors: dict[str, int] = field(default_factory=dict)


_WEIGHTS = {
    "multi_timeframe": 2,
    "abnormal_volatility": 2,
    "data_conflict": 3,
    "data_incomplete": 2,
    "agent_conflict": 3,
    "multi_strategy_comparison": 1,
}


class ComplexityScorer:
    def __init__(self, *, threshold: int = 3) -> None:
        """Args:
            threshold: 达到该分数时,market_analysis 从 fast 升级到 reasoning。
        """
        self.threshold = threshold

    def score(self, ctx: ComplexityInput) -> int:
        total = 0
        for name, weight in _WEIGHTS.items():
            if getattr(ctx, name):
                total += weight
        # extra_factors 只允许非负整数权重,忽略非法值
        for weight in ctx.extra_factors.values():
            if isinstance(weight, int) and not isinstance(weight, bool) and weight > 0:
                total += weight
        return total

    def should_upgrade(self, ctx: ComplexityInput) -> bool:
        """是否应从默认 fast 升级到 reasoning。"""
        return self.score(ctx) >= self.threshold
