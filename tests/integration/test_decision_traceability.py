"""阶段 7 验收:一笔决策全链路可追踪(指导书 13 阶段 7 验收)。

端到端离线:脚本化 LLM + Paper Broker + SQLite 数据库;
决策 → Agent 输出 → 风控审核 → 订单 → 成交 全部入库并可经
`trace_decision(decision_id)` 一次查出;敏感信息不进库、不进日志。
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

# 复用阶段 3/5 夹具
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "unit"))
from test_agents_flow import START, SYMBOL, make_router  # noqa: E402
from test_execute_flow import exec_deps, make_paper  # noqa: E402

from agents.graph import build_graph, run_cycle  # noqa: E402
from agents.nodes import NodeDeps  # noqa: E402
from database.migrations import apply_migrations  # noqa: E402
from database.models import (  # noqa: E402
    AccountSnapshotRow,
    AgentDecisionRow,
    AgentRunRow,
    IndicatorSnapshotRow,
    LLMCallRow,
    MarketSnapshotRow,
    OrderRow,
    PositionRow,
    RiskAuditRow,
    SystemEventRow,
    TradeRow,
    make_engine,
)
from database.repository import Recorder  # noqa: E402
from monitoring.metrics import MetricsCollector  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'trace.db'}")
    apply_migrations(eng)
    rec = Recorder(eng)
    yield rec, eng
    eng.dispose()


async def test_full_cycle_decision_traceable_end_to_end(tmp_path, db):
    """验收:一笔决策可追踪到 Agent 输出、风险审核、订单和成交。"""
    recorder, engine = db
    router, _, _ = make_router()
    broker = make_paper(tmp_path)
    deps0 = exec_deps(router, tmp_path, broker=broker)
    deps = NodeDeps(
        router=deps0.router,
        fetch_series=deps0.fetch_series,
        now_ms=deps0.now_ms,
        audit_path=deps0.audit_path,
        risk_engine=deps0.risk_engine,
        get_account=deps0.get_account,
        order_manager=deps0.order_manager,
        recorder=recorder,
        metrics=MetricsCollector(),
    )
    graph = build_graph(deps)
    state = await run_cycle(
        graph, cycle_id="p7-trace", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["final_status"] == "executed"
    decision_id = "cycle-p7-trace"

    trace = recorder.trace_decision(decision_id)
    assert trace is not None

    # 1. 决策
    dec = trace["decision"]
    assert dec.action == "BUY"
    assert dec.symbol == SYMBOL
    assert dec.amount_usdt == Decimal("10.5")
    assert dec.final_status == "executed"

    # 2. Agent 输出(market/strategy/decision/risk/execution 至少 5 条运行记录)
    agents = {r.agent for r in trace["agent_runs"]}
    assert {"market_agent", "strategy_agent", "decision_agent", "risk_engine", "execution"} <= agents
    assert all(r.decision_id == decision_id for r in trace["agent_runs"])

    # 3. 风险审核
    risk = trace["risk_audits"]
    assert len(risk) == 1
    assert risk[0].verdict == "APPROVED"

    # 4. 订单
    orders = trace["orders"]
    assert len(orders) == 1
    assert orders[0].status == "FILLED"
    assert orders[0].client_order_id == "ai-cycle-p7-trace"
    assert orders[0].filled_qty > 0

    # 5. 成交
    trades = trace["trades"]
    assert len(trades) == 1
    assert trades[0].qty == orders[0].filled_qty

    # 6. LLM 调用(含模型别名/延迟/状态)
    calls = trace["llm_calls"]
    assert len(calls) >= 3  # market + strategy + decision
    assert any(c.model_alias == "fast" for c in calls)
    assert all(c.status in ("ok", "error") for c in calls)

    # 7. 行情 + 指标快照
    snaps = trace["market_snapshots"]
    assert [s.timeframe for s in snaps] == ["1h"]
    assert snaps[0].candle_count == 100
    ind_rows = trace["indicator_snapshots"]
    assert len(ind_rows) == 1
    assert ind_rows[0].rsi14 is not None or ind_rows[0].ma20 is not None

    # 8. 账户快照(risk 时点,买入前无仓位 → positions 为空是正确语义)
    assert len(trace["account_snapshots"]) == 1
    assert trace["account_snapshots"][0].equity_usdt > 0
    assert trace["positions"] == []

    # 第二轮(HOLD):持仓已存在,账户快照应带出仓位 → positions 表可查
    hold_decision = json.dumps(
        {
            "decision_id": "ignored-by-system",
            "symbol": SYMBOL,
            "action": "HOLD",
            "strategy": "TREND_FOLLOWING",
            "amount_usdt": None,
            "confidence": 0.5,
            "reason": "持有",
            "created_at": 1,
            "expires_at": 9_999_999_999_999,
        }
    )
    router2, _, _ = make_router(decision_script=[hold_decision])
    deps2_0 = exec_deps(router2, tmp_path, broker=broker)
    deps2 = NodeDeps(
        router=deps2_0.router,
        fetch_series=deps2_0.fetch_series,
        now_ms=deps2_0.now_ms,
        audit_path=deps2_0.audit_path,
        risk_engine=deps2_0.risk_engine,
        get_account=broker.get_account,
        order_manager=deps2_0.order_manager,
        recorder=recorder,
    )
    graph2 = build_graph(deps2)
    state2 = await run_cycle(
        graph2, cycle_id="p7-trace-hold", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state2["final_status"] == "hold_no_trade"
    trace2 = recorder.trace_decision("cycle-p7-trace-hold")
    assert len(trace2["positions"]) == 1
    assert trace2["positions"][0].symbol == SYMBOL
    assert trace2["positions"][0].qty > 0


async def test_db_failure_degrades_to_jsonl_and_cycle_completes(tmp_path):
    """DB 故障不影响 cycle:record_cycle 抛错,工作流仍走到 executed。"""
    router, _, _ = make_router()
    broker = make_paper(tmp_path)
    deps0 = exec_deps(router, tmp_path, broker=broker)

    class BrokenRecorder:
        def record_cycle(self, *a, **k):
            raise RuntimeError("db down")

        def record_system_event(self, *a, **k):
            return None

    deps = NodeDeps(
        router=deps0.router,
        fetch_series=deps0.fetch_series,
        now_ms=deps0.now_ms,
        audit_path=deps0.audit_path,
        risk_engine=deps0.risk_engine,
        get_account=deps0.get_account,
        order_manager=deps0.order_manager,
        recorder=BrokenRecorder(),
    )
    graph = build_graph(deps)
    state = await run_cycle(
        graph, cycle_id="p7-db-down", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["final_status"] == "executed"
    # JSONL 审计仍完整
    entry = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
    assert entry["order"]["status"] == "FILLED"


async def test_hold_cycle_still_records_decision(tmp_path, db):
    """HOLD 决策也入库(审计完整性)。"""
    recorder, engine = db
    hold_decision = json.dumps(
        {
            "decision_id": "ignored-by-system",
            "symbol": SYMBOL,
            "action": "HOLD",
            "strategy": "TREND_FOLLOWING",
            "amount_usdt": None,
            "confidence": 0.5,
            "reason": "观望",
            "created_at": 1,
            "expires_at": 9_999_999_999_999,
        }
    )
    router, _, _ = make_router(decision_script=[hold_decision])
    broker = make_paper(tmp_path)
    deps0 = exec_deps(router, tmp_path, broker=broker)
    deps = NodeDeps(
        router=deps0.router,
        fetch_series=deps0.fetch_series,
        now_ms=deps0.now_ms,
        audit_path=deps0.audit_path,
        risk_engine=deps0.risk_engine,
        get_account=deps0.get_account,
        order_manager=deps0.order_manager,
        recorder=recorder,
    )
    graph = build_graph(deps)
    state = await run_cycle(
        graph, cycle_id="p7-hold", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["final_status"] == "hold_no_trade"
    trace = recorder.trace_decision("cycle-p7-hold")
    assert trace is not None
    assert trace["decision"].action == "HOLD"
    assert trace["orders"] == []
    assert trace["trades"] == []
    # 风控 NO_ORDER 也记录
    assert trace["risk_audits"][0].verdict == "NO_ORDER"


async def test_order_observer_persists_intent_transitions(tmp_path, db):
    """OrderManager 状态流转经 record_order 入库(单事务 upsert)。"""
    recorder, engine = db
    recorder.record_order(client_order_id="cid-obs", status="CREATED", side="BUY")
    recorder.record_order(client_order_id="cid-obs", status="SUBMITTING")
    recorder.record_order(
        client_order_id="cid-obs", status="FILLED",
        filled_qty=Decimal("0.001"), avg_price=Decimal("50000"),
    )
    with Session(engine) as s:
        row = s.scalar(select(OrderRow).where(OrderRow.client_order_id == "cid-obs"))
        assert row.status == "FILLED"
        assert row.filled_qty == Decimal("0.001")


def test_database_schema_has_no_secrets(tmp_path, db):
    """库内不存任何 API Key/Secret/Token/密码列(指导书 10/16)。"""
    _, engine = db
    insp = inspect(engine)
    # token 计数列(input_tokens/output_tokens)合法;密钥语义列禁止
    banned = {"secret", "password", "authorization", "credential", "token", "key"}
    for table in insp.get_table_names():
        for col in insp.get_columns(table):
            segments = set(col["name"].lower().split("_"))
            hit = segments & banned
            assert not hit, f"{table}.{col['name']} 疑似敏感列 {hit}"


async def test_no_secret_values_in_db_after_full_cycle(tmp_path, db):
    """完整 cycle 后,库内任何文本都不含 API Key 样式字符串。"""
    recorder, engine = db
    router, _, _ = make_router()
    broker = make_paper(tmp_path)
    deps0 = exec_deps(router, tmp_path, broker=broker)
    deps = NodeDeps(
        router=deps0.router,
        fetch_series=deps0.fetch_series,
        now_ms=deps0.now_ms,
        audit_path=deps0.audit_path,
        risk_engine=deps0.risk_engine,
        get_account=deps0.get_account,
        order_manager=deps0.order_manager,
        recorder=recorder,
    )
    graph = build_graph(deps)
    await run_cycle(
        graph, cycle_id="p7-secret", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    with Session(engine) as s:
        all_text: list[str] = []
        for table in (
            AgentDecisionRow, AgentRunRow, OrderRow, TradeRow,
            LLMCallRow, RiskAuditRow, MarketSnapshotRow,
            AccountSnapshotRow, PositionRow, SystemEventRow,
        ):
            for row in s.scalars(select(table)).all():
                all_text.append(str(row.__dict__))
        joined = "\n".join(all_text)
    assert "sk-" not in joined
    assert "Authorization" not in joined
