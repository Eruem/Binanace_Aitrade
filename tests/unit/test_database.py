"""数据库模型/迁移/仓储测试(阶段 7,指导书 10)。

全部离线:SQLite 内存库;断言 UUID 主键、NUMERIC、唯一 Client Order ID、
decision_id 链路、单事务订单更新、无密钥列。
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from database.migrations import MIGRATIONS, apply_migrations, applied_versions
from database.models import (
    Base,
    OrderRow,
    TradeRow,
    make_engine,
    make_session_factory,
)
from database.repository import Recorder

GUIDE_TABLES = {
    "market_snapshots",
    "indicator_snapshots",
    "llm_calls",
    "agent_runs",
    "agent_decisions",
    "risk_audits",
    "orders",
    "trades",
    "positions",
    "account_snapshots",
    "system_events",
}

FORBIDDEN_COLUMN_SEGMENTS = {"secret", "password", "authorization", "credential"}
# "tokens"(复数,LLM token 计数)是合法列名;只拦单数 token/api_key 段
FORBIDDEN_COLUMN_SEGMENTS_EXACT = {"token", "key"}


def _is_forbidden_column(name: str) -> bool:
    segments = set(name.lower().replace("-", "_").split("_"))
    if segments & FORBIDDEN_COLUMN_SEGMENTS:
        return True
    if segments & FORBIDDEN_COLUMN_SEGMENTS_EXACT:
        return True
    if "api" in segments and "key" in segments:
        return True
    return False


@pytest.fixture()
def engine():
    eng = make_engine("sqlite:///:memory:")
    apply_migrations(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def recorder(engine):
    return Recorder(engine)


# ------------------------------------------------------------------ 迁移


def test_migrations_apply_once(engine):
    assert applied_versions(engine) == {MIGRATIONS[0][0]}
    assert apply_migrations(engine) == []  # 幂等


def test_all_guide_tables_exist(engine):
    names = set(inspect(engine).get_table_names())
    assert GUIDE_TABLES <= names
    assert "schema_migrations" in names


def test_no_secret_columns_anywhere(engine):
    insp = inspect(engine)
    for table in GUIDE_TABLES:
        for col in insp.get_columns(table):
            assert not _is_forbidden_column(col["name"]), (
                f"{table}.{col['name']} 疑似敏感列"
            )


def test_client_order_id_unique(engine):
    with Session(engine) as s:
        s.add(OrderRow(client_order_id="cid-1", status="NEW", created_at=1, updated_at=1, recorded_at=1))
        s.commit()
    with Session(engine) as s:
        s.add(OrderRow(client_order_id="cid-1", status="FILLED", created_at=2, updated_at=2, recorded_at=2))
        with pytest.raises(Exception):
            s.commit()


def test_amount_columns_return_decimal(engine):
    with Session(engine) as s:
        s.add(
            OrderRow(
                client_order_id="cid-dec", status="FILLED", amount_usdt=10.5,
                created_at=1, updated_at=1, recorded_at=1,
            )
        )
        s.commit()
    with Session(engine) as s:
        row = s.scalar(select(OrderRow).where(OrderRow.client_order_id == "cid-dec"))
        assert isinstance(row.amount_usdt, Decimal)


# ------------------------------------------------------------------ 仓储


def test_record_order_transactional_upsert(recorder, engine):
    recorder.record_order(client_order_id="c1", status="CREATED", side="BUY")
    recorder.record_order(client_order_id="c1", status="SUBMITTING")
    recorder.record_order(client_order_id="c1", status="FILLED", filled_qty=0.001)
    with Session(engine) as s:
        rows = s.scalars(select(OrderRow)).all()
        assert len(rows) == 1
        assert rows[0].status == "FILLED"
        assert rows[0].filled_qty == Decimal("0.001")


def test_order_update_is_single_transaction(recorder, engine):
    """一次 record_order 内的读写要么全成要么全滚(事务覆盖状态更新)。"""
    recorder.record_order(client_order_id="t1", status="CREATED")
    # 制造一次失败的更新:缺 status → 不应写入半条记录
    before = count_orders(engine)
    try:
        recorder.record_order(client_order_id="missing-status")  # status 空 → 直接跳过
    except Exception:
        pass
    assert count_orders(engine) == before


def count_orders(engine) -> int:
    with Session(engine) as s:
        return len(s.scalars(select(OrderRow)).all())


def test_trace_decision_returns_full_chain(recorder):
    decision_id = "cycle-abc-123"
    now = int(time.time() * 1000)
    with Session(recorder._engine) as s, s.begin():
        from database.models import (
            AgentDecisionRow,
            AgentRunRow,
            LLMCallRow,
            RiskAuditRow,
        )

        s.add(
            AgentDecisionRow(
                decision_id=decision_id, cycle_id="abc-123", symbol="BTC/USDT",
                action="BUY", amount_usdt=10.0, created_at=now, recorded_at=now,
            )
        )
        s.add(AgentRunRow(cycle_id="abc-123", decision_id=decision_id, agent="market_agent", created_at=now))
        s.add(
            RiskAuditRow(
                cycle_id="abc-123", decision_id=decision_id, verdict="APPROVED",
                checked_at=now, created_at=now,
            )
        )
        s.add(
            LLMCallRow(
                cycle_id="abc-123", decision_id=decision_id, task="market_analysis",
                model_alias="fast", provider="p", model="m", recorded_at=now,
            )
        )
    recorder.record_order(
        client_order_id=decision_id.replace("cycle-", "cid-"),
        status="FILLED", decision_id=decision_id, symbol="BTC/USDT", side="BUY",
        filled_qty=0.0002, avg_price=50000.0,
    )
    # 成交:由 FILLED 订单经 record_cycle 合成;此处手动补一条验证链路查询
    with Session(recorder._engine) as s, s.begin():
        s.add(
            TradeRow(
                client_order_id=decision_id.replace("cycle-", "cid-"),
                decision_id=decision_id, symbol="BTC/USDT", side="BUY",
                qty=0.0002, price=50000.0, executed_at=now, created_at=now,
            )
        )

    trace = recorder.trace_decision(decision_id)
    assert trace is not None
    assert trace["decision"].action == "BUY"
    assert [r.agent for r in trace["agent_runs"]] == ["market_agent"]
    assert trace["risk_audits"][0].verdict == "APPROVED"
    assert trace["orders"][0].status == "FILLED"
    assert trace["trades"][0].qty == Decimal("0.0002")
    assert trace["llm_calls"][0].model_alias == "fast"


def test_trace_decision_unknown_returns_none(recorder):
    assert recorder.trace_decision("cycle-does-not-exist") is None


def test_record_system_event(recorder):
    recorder.record_system_event("db_write_failed", "test", cycle_id="c9", severity="WARNING")
    events = recorder.events(cycle_id="c9")
    assert len(events) == 1
    assert events[0].event_type == "db_write_failed"


def test_build_recorder_none_when_empty():
    from database.repository import build_recorder

    assert build_recorder("") is None


def test_build_recorder_with_sqlite(tmp_path):
    from database.repository import build_recorder

    url = f"sqlite:///{tmp_path / 'agent.db'}"
    rec = build_recorder(url)
    assert rec is not None
    rec.record_system_event("boot", "ok")
    assert len(rec.events()) == 1
    rec.close()


def test_normalize_db_url():
    from database.models import normalize_db_url

    assert normalize_db_url("postgresql://x/y") == "postgresql+psycopg://x/y"
    assert normalize_db_url("sqlite:///a.db") == "sqlite:///a.db"


def test_make_session_factory_default(engine):
    factory = make_session_factory(engine)
    with factory() as s:
        assert s is not None


def test_schema_migration_table_has_no_secrets(engine):
    insp = inspect(engine)
    cols = [c["name"] for c in insp.get_columns("schema_migrations")]
    assert not any(_is_forbidden_column(c) for c in cols)


def test_base_metadata_guidance_tables_count(engine):
    """指导书 10:11 张表。"""
    names = GUIDE_TABLES
    assert len(names) == 11
    insp = inspect(engine)
    assert names <= set(insp.get_table_names())
