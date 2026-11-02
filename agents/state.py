"""LangGraph 工作流 State(阶段 3,指导书 6.3)。

并行写入约定(避免冲突):
- market_agent_node 独占写 market_analysis;
- strategy_agent_node 独占写 strategy_proposal;
- decision_agent_node 独占写 trading_decision;
- 会并发追加的列表字段(data_errors / llm_errors / agent_runs)用
  operator.add reducer。
每个 cycle 的字段用 cycle_id / thread_id 关联,可从 Checkpointer 追溯。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from .schemas import AgentRunMeta, MarketAnalysis, StrategyProposal, TradingDecision


class AgentState(TypedDict, total=False):
    # —— 运行标识 ——
    cycle_id: str
    thread_id: str
    symbol: str
    timeframes: list[str]
    started_at: int  # UTC ms

    # —— 行情(由 market_data_node 写入)——
    market_snapshot: Any  # data.MarketSnapshot(frozen dataclass)
    indicator_summaries: dict[str, Any]  # {tf: IndicatorSnapshot}
    data_errors: Annotated[list[str], operator.add]

    # —— Agent 输出(各自独占一个 key)——
    market_analysis: MarketAnalysis | None
    strategy_proposal: StrategyProposal | None
    trading_decision: TradingDecision | None

    # —— 风控(阶段 4;risk_node 独占写)——
    risk_result: Any  # risk.RiskResult
    account_snapshot: Any  # risk.AccountSnapshot(阶段 7 入库/指标用)

    # —— 执行(阶段 5;execute_node 独占写)——
    order_result: Any  # exchange.OrderResult

    # —— 过程审计 ——
    llm_errors: Annotated[list[str], operator.add]
    agent_runs: Annotated[list[AgentRunMeta], operator.add]

    # —— 终态 ——
    # decision_ready | hold_* (阶段 3)
    # risk_approved | risk_rejected (阶段 4 起,配置了风控引擎时)
    # executed | exec_rejected | exec_unknown (阶段 5 起,配置了执行层时)
    final_status: str
    final_note: str
