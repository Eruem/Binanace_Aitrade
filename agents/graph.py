"""LangGraph 工作流组装(阶段 3/4/5,指导书 6.3)。

流程:
    START -> market_data_node
              |-(数据错误)-> decision_agent_node(合成 HOLD)
              `-(正常)--> market_agent_node  } 并行
                         strategy_agent_node }
                                |(都完成后)
                          decision_agent_node
                                |
                            risk_node(阶段 4,配置了风控时)
                                |
                  (APPROVED)-> execute_node(阶段 5,配置了执行层时)
                  (否则)----> record_node
                                |
                            record_node -> END

- Market 与 Strategy 并行分析同一份不可变快照;
- Decision 必须等两者完成;
- 无风控放行绝不执行:execute_node 只在风控引擎与 OrderManager
  同时注入时挂载(fail-safe);
- Checkpointer 持久化状态,thread_id = cycle_id(可追溯);
- Checkpointer 不是订单幂等机制(订单幂等由 OrderManager 负责)。
"""

from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .nodes import (
    NodeDeps,
    decision_agent_node,
    execute_node,
    market_agent_node,
    market_data_node,
    record_node,
    risk_node,
    strategy_agent_node,
)
from .state import AgentState


def _route_after_data(state: AgentState):
    """数据有 error -> 跳过两个分析 Agent,直接进 decision(合成 HOLD);
    否则用 Send 扇出两个并行分析分支。"""
    if state.get("data_errors"):
        return "decision_agent_node"
    return [
        Send("market_agent_node", state),
        Send("strategy_agent_node", state),
    ]


def _route_after_risk(state: AgentState):
    """风控 APPROVED 且携带订单 → execute_node;其余(NO_ORDER/REJECTED)
    直接 record_node(不产生任何订单)。"""
    rs = state.get("risk_result")
    if rs is not None and rs.approved and rs.order is not None:
        return "execute_node"
    return "record_node"


def build_graph(
    deps: NodeDeps, *, checkpointer: Any = None
) -> Any:
    """组装并编译工作流。checkpointer=None 时用内存(仅测试/单轮)。"""
    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()

    graph: StateGraph = StateGraph(AgentState)

    # 用 partial 而不是 lambda:lambda 会丢失"协程函数"标记,
    # langgraph 将其当同步节点调用导致协程从未 await。
    graph.add_node("market_data_node", partial(market_data_node, deps=deps))
    graph.add_node("market_agent_node", partial(market_agent_node, deps=deps))
    graph.add_node("strategy_agent_node", partial(strategy_agent_node, deps=deps))
    graph.add_node("decision_agent_node", partial(decision_agent_node, deps=deps))
    graph.add_node("record_node", partial(record_node, deps=deps))

    with_risk = deps.risk_engine is not None and deps.get_account is not None
    # 无风控放行绝不执行:执行层只在风控同时挂载时接入(fail-safe)
    with_execute = with_risk and deps.order_manager is not None
    if with_risk:
        graph.add_node("risk_node", partial(risk_node, deps=deps))
    if with_execute:
        graph.add_node("execute_node", partial(execute_node, deps=deps))

    graph.add_edge(START, "market_data_node")
    # market_data -> 条件路由(数据错误直达 decision;否则 Send 并行扇出)
    graph.add_conditional_edges(
        "market_data_node",
        _route_after_data,
        ["decision_agent_node", "market_agent_node", "strategy_agent_node"],
    )
    # 两个并行分支都汇入 decision
    graph.add_edge("market_agent_node", "decision_agent_node")
    graph.add_edge("strategy_agent_node", "decision_agent_node")
    if with_execute:
        # decision -> risk(最终否决权)-> (APPROVED) execute | record -> record
        graph.add_edge("decision_agent_node", "risk_node")
        graph.add_conditional_edges(
            "risk_node", _route_after_risk, ["execute_node", "record_node"]
        )
        graph.add_edge("execute_node", "record_node")
    elif with_risk:
        # decision -> risk(最终否决权)-> record
        graph.add_edge("decision_agent_node", "risk_node")
        graph.add_edge("risk_node", "record_node")
    else:
        graph.add_edge("decision_agent_node", "record_node")
    graph.add_edge("record_node", END)

    return graph.compile(checkpointer=checkpointer)


def thread_config(cycle_id: str) -> dict:
    """Checkpointer 使用的线程配置;thread_id 稳定等于 cycle_id。"""
    return {"configurable": {"thread_id": cycle_id}}


async def run_cycle(
    compiled: Any,
    *,
    cycle_id: str,
    symbol: str,
    timeframes: list[str],
    started_at: int,
) -> dict[str, Any]:
    """执行一轮完整工作流,返回最终状态。"""
    initial: AgentState = {
        "cycle_id": cycle_id,
        "thread_id": cycle_id,
        "symbol": symbol,
        "timeframes": timeframes,
        "started_at": started_at,
        "data_errors": [],
        "llm_errors": [],
        "agent_runs": [],
    }
    result = await compiled.ainvoke(initial, config=thread_config(cycle_id))
    return result
