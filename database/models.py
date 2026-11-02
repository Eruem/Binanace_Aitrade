"""数据库模型(阶段 7,指导书 10)。

11 张审计/运行表 + 迁移记录表;UUID 字符串主键;金额、价格、数量一律
NUMERIC(Decimal);时间统一 UTC 毫秒时间戳;订单表对 Client Order ID
加唯一约束;全部决策相关表带 decision_id 索引。

安全边界:任何表都不保存 API Key、Secret、Authorization、Telegram
Token 或数据库口令(测试断言列名与内容)。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# NUMERIC 精度:交易所数量/价格足够用
AMOUNT = Numeric(24, 8)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class SchemaMigration(Base):
    """已应用的迁移版本(轻量迁移记录,不用 Alembic)。"""

    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms


class MarketSnapshotRow(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(16))
    source: Mapped[str | None] = mapped_column(String(32), default=None)
    candle_count: Mapped[int | None] = mapped_column(Integer, default=None)
    first_open_time: Mapped[int | None] = mapped_column(BigInteger, default=None)
    last_close_time: Mapped[int | None] = mapped_column(BigInteger, default=None)
    fetched_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms
    # 最近收盘价摘要(不存全量 OHLCV,避免体积失控)
    closes_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms


class IndicatorSnapshotRow(Base):
    __tablename__ = "indicator_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(16))
    last_close: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    ma20: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    ma60: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    rsi14: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    macd_dif: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    macd_dea: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    macd_hist: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    atr14: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    bb_mid: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    bb_upper: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    bb_lower: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    volume_ma20: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    volume_roc1: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    computed_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms
    created_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms


class LLMCallRow(Base):
    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    task: Mapped[str] = mapped_column(String(32))
    model_alias: Mapped[str | None] = mapped_column(String(64), default=None)
    provider: Mapped[str | None] = mapped_column(String(64), default=None)
    model: Mapped[str | None] = mapped_column(String(128), default=None)
    routing_reason: Mapped[str | None] = mapped_column(Text, default=None)
    latency_ms: Mapped[float | None] = mapped_column(Float, default=None)
    input_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    output_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str | None] = mapped_column(String(32), default=None)
    error_kind: Mapped[str | None] = mapped_column(String(64), default=None)
    request_id: Mapped[str | None] = mapped_column(String(128), default=None)
    recorded_at: Mapped[int] = mapped_column(BigInteger, index=True)  # UTC ms


class AgentRunRow(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    agent: Mapped[str] = mapped_column(String(32))
    task: Mapped[str | None] = mapped_column(String(64), default=None)
    model_alias: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str | None] = mapped_column(String(32), default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms


class AgentDecisionRow(Base):
    __tablename__ = "agent_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    decision_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(8))
    strategy: Mapped[str | None] = mapped_column(String(64), default=None)
    amount_usdt: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    confidence: Mapped[float | None] = mapped_column(Float, default=None)
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    final_status: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms(决策自身时间)
    expires_at: Mapped[int | None] = mapped_column(BigInteger, default=None)
    recorded_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms(入库时间)


class RiskAuditRow(Base):
    __tablename__ = "risk_audits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    decision_id: Mapped[str] = mapped_column(String(64), index=True)
    verdict: Mapped[str] = mapped_column(String(16))
    reasons: Mapped[list[Any] | None] = mapped_column(JSON, default=None)
    checks: Mapped[list[Any] | None] = mapped_column(JSON, default=None)
    sized_amount_usdt: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    checked_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms
    created_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    cycle_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    decision_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    symbol: Mapped[str | None] = mapped_column(String(32), default=None)
    side: Mapped[str | None] = mapped_column(String(8), default=None)
    order_type: Mapped[str | None] = mapped_column(String(32), default=None)
    status: Mapped[str] = mapped_column(String(32))
    amount_usdt: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    filled_qty: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    avg_price: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    fee: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    fee_currency: Mapped[str | None] = mapped_column(String(16), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms
    updated_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms
    recorded_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms


class TradeRow(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    client_order_id: Mapped[str] = mapped_column(String(64), index=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[float] = mapped_column(AMOUNT)
    price: Mapped[float] = mapped_column(AMOUNT)
    fee: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    fee_currency: Mapped[str | None] = mapped_column(String(16), default=None)
    executed_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms
    created_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms


class PositionRow(Base):
    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    symbol: Mapped[str] = mapped_column(String(32))
    qty: Mapped[float] = mapped_column(AMOUNT)
    entry_price: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    value_usdt: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    recorded_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms


class AccountSnapshotRow(Base):
    __tablename__ = "account_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    equity_usdt: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    available_usdt: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    open_exposure_usdt: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    open_orders_locked_usdt: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    daily_realized_pnl_usdt: Mapped[float | None] = mapped_column(AMOUNT, default=None)
    consecutive_losses: Mapped[int | None] = mapped_column(Integer, default=None)
    fetched_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms
    recorded_at: Mapped[int] = mapped_column(BigInteger)  # UTC ms


class SystemEventRow(Base):
    __tablename__ = "system_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cycle_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    decision_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    event_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(16), default="INFO")
    created_at: Mapped[int] = mapped_column(BigInteger, index=True)  # UTC ms


def normalize_db_url(url: str) -> str:
    """把 postgresql:// 补上 psycopg 驱动,其余原样返回。"""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def make_engine(url: str) -> Engine:
    """按 URL 创建引擎(生产 PostgreSQL/psycopg,测试 SQLite)。"""
    from pathlib import Path

    from sqlalchemy import create_engine

    normalized = normalize_db_url(url)
    # SQLite 文件库:自动创建父目录(相对路径相对当前工作目录)
    if normalized.startswith("sqlite:///") and not normalized.startswith("sqlite:///:memory:"):
        db_path = normalized[len("sqlite:///") :]
        if db_path and db_path != ":memory:":
            Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    return create_engine(normalized, future=True, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
