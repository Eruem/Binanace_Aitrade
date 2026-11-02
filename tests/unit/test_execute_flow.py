"""阶段 5:执行接入工作流测试(decision -> risk -> execute -> record)。

全部离线:假行情 + 脚本化 Provider + PaperBroker 本地模拟成交。
"""

from __future__ import annotations

import json
from decimal import Decimal

from agents.graph import build_graph, run_cycle
from agents.nodes import NodeDeps
from config.loader import RiskParams
from exchange.interfaces import Broker
from exchange.order_manager import OrderManager
from exchange.order_state import OrderStatus
from exchange.paper_broker import PaperBroker
from risk.engine import RiskEngine

from test_agents_flow import DECISION_JSON, START, SYMBOL, H, make_deps, make_router

D = Decimal
REF = D("50000")


def make_paper(tmp_path, **over) -> PaperBroker:
    kw = dict(
        price_provider=lambda s: REF,
        state_path=tmp_path / "paper.json",
        fee_rate="0.001",
        slippage_rate="0.001",
    )
    kw.update(over)
    return PaperBroker(**kw)


def exec_deps(
    router,
    tmp_path,
    *,
    broker: Broker | None = None,
    fill_ratio: str = "1",
    with_manager: bool = True,
) -> NodeDeps:
    """风控 + Paper Broker 全链路依赖(决策 BUY 10.5)。"""
    paper = broker or make_paper(tmp_path, fill_ratio=fill_ratio)
    engine = RiskEngine(
        params=RiskParams(),
        allowed_symbols=(SYMBOL,),
        audit_path=tmp_path / "risk.jsonl",
    )
    manager = (
        OrderManager(paper, state_path=tmp_path / "intents.json")
        if with_manager
        else None
    )
    deps = make_deps(router, audit_path=tmp_path / "audit.jsonl")
    return NodeDeps(
        router=deps.router,
        fetch_series=deps.fetch_series,
        now_ms=deps.now_ms,
        audit_path=deps.audit_path,
        risk_engine=engine,
        get_account=paper.get_account,
        order_manager=manager,
    )


async def test_approved_buy_executes_full_fill(tmp_path):
    """验收:风控放行的 BUY 走完 decision→risk→execute→record,全额成交。"""
    router, _, _ = make_router()
    broker = make_paper(tmp_path)
    deps = exec_deps(router, tmp_path, broker=broker)
    graph = build_graph(deps)
    state = await run_cycle(
        graph, cycle_id="e-ok", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["final_status"] == "executed"
    order = state["order_result"]
    assert order.status == OrderStatus.FILLED
    assert order.side == "BUY"
    assert order.filled_qty > 0
    assert order.client_order_id == "ai-cycle-e-ok"  # 稳定 ID,由 decision_id 派生
    # 风控已注册防重复
    assert deps.risk_engine.was_executed("cycle-e-ok")
    # audit 流水含订单
    entry = json.loads(
        (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip()
    )
    assert entry["order"] is not None
    assert entry["order"]["status"] == "FILLED"
    assert entry["risk"]["verdict"] == "APPROVED"


async def test_approved_sell_executes(tmp_path):
    """验收:风控放行的 SELL 也走执行;先买后卖一轮(决策脚本化)。"""
    router, _, _ = make_router()
    # 先跑一轮 BUY 建仓
    broker = make_paper(tmp_path)
    deps = exec_deps(router, tmp_path, broker=broker)
    graph = build_graph(deps)
    buy_state = await run_cycle(
        graph, cycle_id="e-b", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert buy_state["final_status"] == "executed"

    # 再跑一轮 SELL(独立 cycle_id,脚本化 SELL 决策)
    sell_decision = json.dumps(
        {
            "decision_id": "ignored-by-system",
            "symbol": SYMBOL,
            "action": "SELL",
            "strategy": "TREND_FOLLOWING",
            "amount_usdt": "5",
            "confidence": 0.6,
            "reason": "止盈",
            "created_at": 1,
            "expires_at": 9_999_999_999_999,
        }
    )
    router2, _, _ = make_router(decision_script=[sell_decision])
    deps2 = exec_deps(router2, tmp_path)
    # 卖出走同一个 Paper 状态文件,持仓继承自上一轮
    deps2 = NodeDeps(
        router=deps2.router,
        fetch_series=deps2.fetch_series,
        now_ms=deps2.now_ms,
        audit_path=deps2.audit_path,
        risk_engine=deps2.risk_engine,
        get_account=broker.get_account,
        order_manager=deps2.order_manager,
    )
    graph2 = build_graph(deps2)
    sell_state = await run_cycle(
        graph2, cycle_id="e-s", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert sell_state["final_status"] == "executed"
    assert sell_state["order_result"].side == "SELL"
    assert sell_state["order_result"].status == OrderStatus.FILLED


async def test_risk_rejected_never_reaches_broker(tmp_path):
    """风控否决(余额不足)→ 不执行,Broker 无任何订单。"""
    router, _, _ = make_router()
    broker = make_paper(tmp_path, initial_balances={"USDT": "1"})
    deps = exec_deps(router, tmp_path, broker=broker)
    graph = build_graph(deps)
    state = await run_cycle(
        graph, cycle_id="e-rej", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["final_status"] == "risk_rejected"
    assert state.get("order_result") is None
    assert await broker.get_order("ai-cycle-e-rej") is None
    assert not deps.risk_engine.was_executed("cycle-e-rej")


async def test_partial_fill_flow(tmp_path):
    """部分成交模拟接入工作流:订单 PARTIALLY_FILLED,锁定余额。"""
    router, _, _ = make_router()
    broker = make_paper(tmp_path, fill_ratio="0.4")
    deps = exec_deps(router, tmp_path, broker=broker, fill_ratio="0.4")
    graph = build_graph(deps)
    state = await run_cycle(
        graph, cycle_id="e-part", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["final_status"] == "executed"
    order = state["order_result"]
    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.filled_qty < order.requested_qty
    account = await broker.get_account()
    assert account.open_orders_locked_usdt > 0


async def test_duplicate_cycle_submission_is_idempotent(tmp_path):
    """同 decision 再次执行不会重复记账(Manager + Broker 双重幂等)。"""
    router, _, _ = make_router()
    broker = make_paper(tmp_path)
    deps = exec_deps(router, tmp_path, broker=broker)
    graph = build_graph(deps)
    await run_cycle(
        graph, cycle_id="e-dup", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    # 直接模拟重复提交同一 decision 的订单(绕过图,直打 Manager)
    order = state_order_for("cycle-e-dup")
    before = (await broker.get_account()).available_usdt
    again = await deps.order_manager.submit(order)
    assert again.status == OrderStatus.FILLED
    assert len(broker.trades()) == 1  # 仍只有一笔成交
    after = (await broker.get_account()).available_usdt
    assert after == before  # 没有第二次扣款


def state_order_for(decision_id: str):
    from risk.order_validator import OrderRequest

    return OrderRequest(
        decision_id=decision_id, symbol=SYMBOL, side="BUY", amount_usdt=D("10.5")
    )


async def test_broker_exception_marks_unknown(tmp_path):
    """Broker 提交异常 → final_status=exec_unknown,已注册防重复(结果未知)。"""

    inner = make_paper(tmp_path)  # 账户快照正常,仅提交通道故障

    class BrokenBroker(Broker):
        name = "broken"

        async def submit_order(self, request, *, client_order_id):
            raise TimeoutError("模拟断线")

        async def get_order(self, client_order_id, *, symbol=None):
            return None

        async def cancel_order(self, client_order_id, *, symbol=None):
            raise NotImplementedError

        async def get_account(self):
            return await inner.get_account()

    router, _, _ = make_router()
    deps = exec_deps(router, tmp_path, broker=BrokenBroker())
    graph = build_graph(deps)
    state = await run_cycle(
        graph, cycle_id="e-unk", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["final_status"] == "exec_unknown"
    assert state["order_result"].status == OrderStatus.UNKNOWN
    # 结果未知也注册:宁可阻止重复,不可重复下单
    assert deps.risk_engine.was_executed("cycle-e-unk")
    runs = [r for r in state["agent_runs"] if r.agent == "execution"]
    assert runs and runs[-1].status == "unknown"


async def test_execute_without_risk_approval_rejects(tmp_path):
    """误挂 execute_node 但无风控放行 → exec_rejected(不会下单)。"""
    from agents.nodes import execute_node
    from agents.schemas import Action, StrategyName, TradingDecision

    router, _, _ = make_router()
    deps = exec_deps(router, tmp_path)
    decision = TradingDecision(
        decision_id="cycle-xx", symbol=SYMBOL, action=Action.BUY,
        strategy=StrategyName.TREND_FOLLOWING, amount_usdt=D("10"),
        confidence=0.5, reason="test", created_at=START,
        expires_at=START + H,
    )
    out = await execute_node(
        {"trading_decision": decision},  # 无 risk_result
        deps,
    )
    assert out["final_status"] == "exec_rejected"
    # Manager 未被触碰:没有任何订单产生
    broker = make_paper(tmp_path)
    assert await broker.get_order("ai-cycle-xx") is None


async def test_no_manager_no_execute_node(tmp_path):
    """未注入 OrderManager:不挂载 execute_node,流程停在 risk_approved。"""
    router, _, _ = make_router()
    deps = exec_deps(router, tmp_path, with_manager=False)
    graph = build_graph(deps)
    state = await run_cycle(
        graph, cycle_id="e-nomgr", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["final_status"] == "risk_approved"
    assert state.get("order_result") is None
