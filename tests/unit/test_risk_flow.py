"""阶段 4:风控接入工作流测试(decision -> risk -> record)。

全部离线:假行情 + 脚本化 Provider + 注入假账户,不发任何网络请求。
"""

from __future__ import annotations

import json
from decimal import Decimal

from agents.graph import build_graph, run_cycle
from agents.nodes import NodeDeps
from config.loader import RiskParams
from risk.daily_loss import DailyLossTracker
from risk.engine import AccountSnapshot, Position, RiskEngine
from risk.kill_switch import KillSwitch

from test_agents_flow import (
    DECISION_JSON,
    START,
    SYMBOL,
    H,
    make_deps,
    make_router,
)

D = Decimal


def make_account(**over) -> AccountSnapshot:
    base = dict(
        equity_usdt=D("1000"),
        available_usdt=D("1000"),
        fetched_at=START + 100 * H,
    )
    base.update(over)
    return AccountSnapshot(**base)


def risk_deps(
    router,
    *,
    account: AccountSnapshot | None = None,
    account_error: Exception | None = None,
    kill_switch: KillSwitch | None = None,
    daily_loss: DailyLossTracker | None = None,
    params: RiskParams | None = None,
    audit_path=None,
    risk_audit_path=None,
) -> NodeDeps:
    engine = RiskEngine(
        params=params or RiskParams(),
        allowed_symbols=(SYMBOL,),
        kill_switch=kill_switch,
        daily_loss=daily_loss,
        audit_path=risk_audit_path,
    )

    async def get_account() -> AccountSnapshot:
        if account_error is not None:
            raise account_error
        return account or make_account()

    deps = make_deps(router, audit_path=audit_path)
    return NodeDeps(
        router=deps.router,
        fetch_series=deps.fetch_series,
        now_ms=deps.now_ms,
        audit_path=deps.audit_path,
        risk_engine=engine,
        get_account=get_account,
    )


async def test_buy_risk_approved_with_sized_amount():
    router, _, _ = make_router()
    graph = build_graph(risk_deps(router))
    state = await run_cycle(
        graph, cycle_id="r-ok", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["final_status"] == "risk_approved"
    result = state["risk_result"]
    assert result.verdict == "APPROVED"
    # 决策 10.5,全部上限都大于它 -> 按决策金额放行
    assert result.sized_amount_usdt == D("10.50")
    assert result.order is not None and result.order.side == "BUY"
    assert "风控通过" in state["final_note"]
    # risk_node 的 agent_run 被记录
    assert any(r.agent == "risk_engine" and r.status == "approved"
               for r in state["agent_runs"])


async def test_insufficient_balance_risk_rejected():
    router, _, _ = make_router()
    # 可用余额 1 USDT:仓位计算 -> ~0.99 < 最小名义 5 -> 订单验证拒绝
    deps = risk_deps(router, account=make_account(available_usdt=D("1")))
    graph = build_graph(deps)
    state = await run_cycle(
        graph, cycle_id="r-poor", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["final_status"] == "risk_rejected"
    result = state["risk_result"]
    assert result.verdict == "REJECTED"
    assert state["final_note"]  # 携带逐条否决原因


async def test_kill_switch_blocks_new_buy():
    router, _, _ = make_router()
    ks = KillSwitch()
    ks.auto_pause("接口异常测试")
    graph = build_graph(risk_deps(router, kill_switch=ks))
    state = await run_cycle(
        graph, cycle_id="r-ks", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["final_status"] == "risk_rejected"
    assert any(c.rule == "kill_switch" and not c.passed
               for c in state["risk_result"].checks)


async def test_daily_loss_blocks_new_buy():
    router, _, _ = make_router()
    tracker = DailyLossTracker()
    tracker.record(D("-25"))  # 超过 1000×2% = 20
    graph = build_graph(risk_deps(router, daily_loss=tracker))
    state = await run_cycle(
        graph, cycle_id="r-dl", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["final_status"] == "risk_rejected"


async def test_account_failure_rejects_fail_safe():
    router, _, _ = make_router()
    deps = risk_deps(router, account_error=RuntimeError("账户接口不可用"))
    graph = build_graph(deps)
    state = await run_cycle(
        graph, cycle_id="r-acct-err", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["final_status"] == "risk_rejected"
    assert "账户快照获取失败" in state["final_note"]
    assert "risk_result" not in state or state.get("risk_result") is None


async def test_data_error_hold_passes_risk_as_no_order():
    """数据错误 -> 合成 HOLD -> 风控 NO_ORDER(不产生任何订单)。"""
    from test_agents_flow import gappy_series_map

    router, _, _ = make_router()

    async def fetch_gappy(symbol, tfs):
        return gappy_series_map()

    deps = risk_deps(router)
    deps = NodeDeps(
        router=deps.router,
        fetch_series=fetch_gappy,
        now_ms=deps.now_ms,
        risk_engine=deps.risk_engine,
        get_account=deps.get_account,
    )
    graph = build_graph(deps)
    state = await run_cycle(
        graph, cycle_id="r-data-err", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["trading_decision"].action.value == "HOLD"
    # NO_ORDER 不是放行也不是成交:final_status 保持 hold 语义
    assert state["final_status"] in {"hold_data_error", "risk_no_order"}
    assert state["risk_result"].verdict in {"NO_ORDER", "REJECTED"}


async def test_risk_audit_recorded_in_cycle_audit(tmp_path):
    router, _, _ = make_router()
    # 风控自审与 cycle 审计是两条独立流水,分开落盘
    deps = risk_deps(
        router,
        audit_path=tmp_path / "audit.jsonl",
        risk_audit_path=tmp_path / "risk.jsonl",
    )
    graph = build_graph(deps)
    await run_cycle(
        graph, cycle_id="r-audit", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    lines = (tmp_path / "audit.jsonl").read_text(
        encoding="utf-8"
    ).strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["final_status"] == "risk_approved"
    assert entry["risk"] is not None
    assert entry["risk"]["verdict"] == "APPROVED"
    assert entry["risk"]["sized_amount_usdt"] == "10.50"
    assert entry["decision"]["decision_id"] == "cycle-r-audit"
    # 风控自审流水也存在
    risk_lines = (tmp_path / "risk.jsonl").read_text(
        encoding="utf-8"
    ).strip().splitlines()
    assert len(risk_lines) == 1
    assert json.loads(risk_lines[0])["verdict"] == "APPROVED"


async def test_without_risk_engine_graph_still_works():
    """未注入风控时(阶段 3 兼容):流程照常,final_status 停在 decision_ready。"""
    router, _, _ = make_router()
    graph = build_graph(make_deps(router))
    state = await run_cycle(
        graph, cycle_id="r-no-risk", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["final_status"] == "decision_ready"
    assert state.get("risk_result") is None
