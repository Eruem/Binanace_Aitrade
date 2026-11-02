"""数据访问层(阶段 7,指导书 10)。

`Recorder` 把一个 cycle 的全部审计数据(行情快照、指标、Agent 运行、
LLM 调用、决策、风控审核、订单、成交、账户快照、系统事件)写入数据库;
订单意图/状态更新在**单个事务**内完成(指导书 10 的事务要求)。

`trace_decision(decision_id)` 返回贯穿
决策 → Agent 输出 → 风控审核 → 订单 → 成交 的完整链路。

安全边界:写入内容不含 API Key/Secret/Authorization(调用方也不传入);
数据库不可用时上层记录警告并继续(JSONL 审计是主备份,DB 是增强)。
"""

from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from database.models import (
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
    make_session_factory,
)
from database.migrations import apply_migrations

_log = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _to_amount(value: Any) -> float | None:
    """Decimal/str/float → float(Decimal 先转 str 保精度路径清晰)。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(Decimal(str(value)))
    except Exception:
        return None


def _int_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


class Recorder:
    """cycle 级审计数据的写入/查询入口。

    - record_cycle:一个事务写入该 cycle 的全部快照(失败即回滚并抛出,
      由调用方降级为仅 JSONL);
    - record_order:订单意图/状态更新的事务入口(观察者模式,来自
      OrderManager 的每次状态落盘);
    - trace_decision:决策全链路查询。
    """

    def __init__(self, engine: Engine, *, session_factory: sessionmaker | None = None):
        self._engine = engine
        self._sessions = session_factory or make_session_factory(engine)

    # ------------------------------------------------------------------ 写入

    def record_cycle(self, state: dict[str, Any], usage_records: list[Any] | None = None) -> None:
        """把一个 cycle 的终态写库;整体一个事务,异常上抛(调用方降级)。"""
        now = _now_ms()
        cycle_id = state.get("cycle_id", "")
        decision = state.get("trading_decision")
        decision_id = getattr(decision, "decision_id", None) if decision else None

        with self._sessions.begin() as session:
            # 1. 决策(可空:合成 HOLD 也可能没有;此处以 state 为准)
            if decision is not None and decision_id:
                session.add(
                    AgentDecisionRow(
                        decision_id=decision_id,
                        cycle_id=cycle_id,
                        symbol=getattr(decision, "symbol", None) or state.get("symbol", ""),
                        action=getattr(decision, "action", "HOLD"),
                        strategy=getattr(decision, "strategy", None),
                        amount_usdt=_to_amount(getattr(decision, "amount_usdt", None)),
                        confidence=getattr(decision, "confidence", None),
                        reason=getattr(decision, "reason", None),
                        final_status=state.get("final_status"),
                        created_at=_int_ms(getattr(decision, "created_at", now)) or now,
                        expires_at=_int_ms(getattr(decision, "expires_at", None)),
                        recorded_at=now,
                    )
                )

            # 2. Agent 运行记录
            for run in state.get("agent_runs") or []:
                if isinstance(run, dict):
                    agent, task = run.get("agent"), run.get("task")
                    alias, status, note = run.get("model_alias"), run.get("status"), run.get("note")
                else:
                    agent, task = getattr(run, "agent", "?"), getattr(run, "task", None)
                    alias = getattr(run, "model_alias", None)
                    status, note = getattr(run, "status", None), getattr(run, "note", None)
                session.add(
                    AgentRunRow(
                        cycle_id=cycle_id,
                        decision_id=decision_id,
                        agent=agent or "?",
                        task=task,
                        model_alias=alias,
                        status=status,
                        note=note,
                        created_at=now,
                    )
                )

            # 3. 行情 + 指标快照
            market = state.get("market_snapshot")
            indicator_map = state.get("indicator_summaries") or {}
            if market is not None:
                for tf, series in _extract_timeframes(market):
                    closes = _tail_closes(series)
                    session.add(
                        MarketSnapshotRow(
                            cycle_id=cycle_id,
                            decision_id=decision_id,
                            symbol=state.get("symbol", ""),
                            timeframe=tf,
                            source=_tf_source(series),
                            candle_count=_tf_candle_count(series),
                            first_open_time=_tf_open_time(series),
                            last_close_time=_tf_close_time(series),
                            fetched_at=_tf_fetched_at(series, now),
                            closes_json={"closes": closes} if closes else None,
                            created_at=now,
                        )
                    )
                    snap = indicator_map.get(tf)
                    if snap is not None:
                        session.add(
                            IndicatorSnapshotRow(
                                cycle_id=cycle_id,
                                decision_id=decision_id,
                                symbol=getattr(snap, "symbol", None) or state.get("symbol", ""),
                                timeframe=tf,
                                **_indicator_fields(snap),
                                computed_at=_tf_fetched_at(series, now),
                                created_at=now,
                            )
                        )

            # 4. LLM 调用(UsageRecord.recorded_at 是秒级 float)
            for rec in usage_records or []:
                rec_at = getattr(rec, "recorded_at", None)
                recorded_at = int(rec_at * 1000) if isinstance(rec_at, float) else (_int_ms(rec_at) or now)
                session.add(
                    LLMCallRow(
                        cycle_id=getattr(rec, "cycle_id", cycle_id) or cycle_id,
                        decision_id=decision_id,
                        task=getattr(rec, "task", "") or "",
                        model_alias=getattr(rec, "model_alias", None),
                        provider=getattr(rec, "provider", None),
                        model=getattr(rec, "model", None),
                        latency_ms=getattr(rec, "latency_ms", None),
                        input_tokens=getattr(rec, "input_tokens", None),
                        output_tokens=getattr(rec, "output_tokens", None),
                        status=getattr(rec, "status", None),
                        error_kind=getattr(rec, "error_kind", None),
                        request_id=getattr(rec, "request_id", None),
                        recorded_at=recorded_at,
                    )
                )

            # 5. 风控审核
            risk = state.get("risk_result")
            if risk is not None:
                risk_dict = risk.to_audit_dict() if hasattr(risk, "to_audit_dict") else {}
                session.add(
                    RiskAuditRow(
                        cycle_id=cycle_id,
                        decision_id=getattr(risk, "decision_id", None) or decision_id or "",
                        verdict=getattr(risk, "verdict", "?"),
                        reasons=getattr(risk, "reasons", None),
                        checks=risk_dict.get("checks"),
                        sized_amount_usdt=_to_amount(getattr(risk, "sized_amount_usdt", None)),
                        checked_at=_int_ms(getattr(risk, "checked_at", None)) or now,
                        created_at=now,
                    )
                )

            # 6. 订单 + 成交(同一事务:指导书 10 的事务要求)
            order = state.get("order_result")
            if order is not None:
                self._upsert_order(session, order, cycle_id=cycle_id, decision_id=decision_id, now=now)
                trade = _trade_from_order(order, decision_id=decision_id, now=now)
                if trade is not None:
                    session.add(trade)

            # 7. 账户快照 + 仓位
            account = state.get("account_snapshot")
            if account is not None:
                session.add(
                    AccountSnapshotRow(
                        cycle_id=cycle_id,
                        decision_id=decision_id,
                        equity_usdt=_to_amount(getattr(account, "equity_usdt", None)),
                        available_usdt=_to_amount(getattr(account, "available_usdt", None)),
                        open_exposure_usdt=_to_amount(getattr(account, "open_exposure_usdt", None)),
                        open_orders_locked_usdt=_to_amount(
                            getattr(account, "open_orders_locked_usdt", None)
                        ),
                        daily_realized_pnl_usdt=_to_amount(
                            getattr(account, "daily_realized_pnl_usdt", None)
                        ),
                        consecutive_losses=getattr(account, "consecutive_losses", None),
                        fetched_at=_int_ms(getattr(account, "fetched_at", None)) or now,
                        recorded_at=now,
                    )
                )
                positions = getattr(account, "positions", None) or {}
                items = positions.items() if isinstance(positions, dict) else [
                    (getattr(p, "symbol", "?"), p) for p in positions
                ]
                for sym, pos in items:
                    session.add(
                        PositionRow(
                            cycle_id=cycle_id,
                            decision_id=decision_id,
                            symbol=sym,
                            qty=_to_amount(getattr(pos, "qty", None)) or 0.0,
                            entry_price=_to_amount(getattr(pos, "entry_price", None)),
                            value_usdt=_to_amount(getattr(pos, "value_usdt", None)),
                            recorded_at=now,
                        )
                    )

            # 8. 系统事件(数据错误/LLM 错误)
            for err in state.get("data_errors") or []:
                session.add(
                    SystemEventRow(
                        cycle_id=cycle_id,
                        decision_id=decision_id,
                        event_type="data_error",
                        message=str(err)[:2000],
                        severity="ERROR",
                        created_at=now,
                    )
                )
            for err in state.get("llm_errors") or []:
                session.add(
                    SystemEventRow(
                        cycle_id=cycle_id,
                        decision_id=decision_id,
                        event_type="llm_error",
                        message=str(err)[:2000],
                        severity="WARNING",
                        created_at=now,
                    )
                )

    def record_order(
        self,
        *,
        client_order_id: str,
        status: str,
        cycle_id: str | None = None,
        decision_id: str | None = None,
        symbol: str | None = None,
        side: str | None = None,
        order_type: str | None = None,
        amount_usdt: Any = None,
        filled_qty: Any = None,
        avg_price: Any = None,
        fee: Any = None,
        fee_currency: str | None = None,
        exchange_order_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """单个订单意图/状态更新的事务入口。

        以 client_order_id 为幂等键:存在即更新,不存在即插入。
        """
        now = _now_ms()
        with self._sessions.begin() as session:
            self._upsert_order(
                session,
                None,
                cycle_id=cycle_id,
                decision_id=decision_id,
                now=now,
                client_order_id=client_order_id,
                status=status,
                symbol=symbol,
                side=side,
                order_type=order_type,
                amount_usdt=amount_usdt,
                filled_qty=filled_qty,
                avg_price=avg_price,
                fee=fee,
                fee_currency=fee_currency,
                exchange_order_id=exchange_order_id,
                error=error,
            )

    def record_system_event(
        self,
        event_type: str,
        message: str,
        *,
        cycle_id: str | None = None,
        decision_id: str | None = None,
        severity: str = "INFO",
    ) -> None:
        with self._sessions.begin() as session:
            session.add(
                SystemEventRow(
                    cycle_id=cycle_id,
                    decision_id=decision_id,
                    event_type=event_type,
                    message=str(message)[:2000],
                    severity=severity,
                    created_at=_now_ms(),
                )
            )

    # ------------------------------------------------------------------ 查询

    def trace_decision(self, decision_id: str) -> dict[str, Any] | None:
        """决策全链路:决策 → Agent 输出 → 风控 → 订单 → 成交 + LLM 调用。

        决策不存在返回 None。
        """
        with self._sessions() as session:
            decision = session.scalar(
                select(AgentDecisionRow).where(AgentDecisionRow.decision_id == decision_id)
            )
            if decision is None:
                return None
            runs = session.scalars(
                select(AgentRunRow)
                .where(AgentRunRow.decision_id == decision_id)
                .order_by(AgentRunRow.created_at)
            ).all()
            risk = session.scalars(
                select(RiskAuditRow)
                .where(RiskAuditRow.decision_id == decision_id)
                .order_by(RiskAuditRow.created_at)
            ).all()
            orders = session.scalars(
                select(OrderRow).where(OrderRow.decision_id == decision_id).order_by(OrderRow.created_at)
            ).all()
            trades = session.scalars(
                select(TradeRow).where(TradeRow.decision_id == decision_id).order_by(TradeRow.executed_at)
            ).all()
            llm_calls = session.scalars(
                select(LLMCallRow)
                .where(LLMCallRow.decision_id == decision_id)
                .order_by(LLMCallRow.recorded_at)
            ).all()
            snapshots = session.scalars(
                select(MarketSnapshotRow)
                .where(MarketSnapshotRow.decision_id == decision_id)
                .order_by(MarketSnapshotRow.created_at)
            ).all()
            account = session.scalars(
                select(AccountSnapshotRow)
                .where(AccountSnapshotRow.decision_id == decision_id)
                .order_by(AccountSnapshotRow.recorded_at)
            ).all()
            positions = session.scalars(
                select(PositionRow)
                .where(PositionRow.decision_id == decision_id)
                .order_by(PositionRow.recorded_at)
            ).all()
            events = session.scalars(
                select(SystemEventRow)
                .where(SystemEventRow.decision_id == decision_id)
                .order_by(SystemEventRow.created_at)
            ).all()
            indicators = session.scalars(
                select(IndicatorSnapshotRow)
                .where(IndicatorSnapshotRow.decision_id == decision_id)
                .order_by(IndicatorSnapshotRow.created_at)
            ).all()
            return {
                "decision": decision,
                "agent_runs": list(runs),
                "risk_audits": list(risk),
                "orders": list(orders),
                "trades": list(trades),
                "llm_calls": list(llm_calls),
                "market_snapshots": list(snapshots),
                "indicator_snapshots": list(indicators),
                "account_snapshots": list(account),
                "positions": list(positions),
                "system_events": list(events),
            }

    def events(self, *, cycle_id: str | None = None, limit: int = 100) -> list[SystemEventRow]:
        with self._sessions() as session:
            stmt = select(SystemEventRow).order_by(SystemEventRow.created_at.desc()).limit(limit)
            if cycle_id:
                stmt = stmt.where(SystemEventRow.cycle_id == cycle_id)
            return list(session.scalars(stmt).all())

    def close(self) -> None:
        self._engine.dispose()

    # ------------------------------------------------------------------ 内部

    def _upsert_order(
        self,
        session: Session,
        order: Any,
        *,
        cycle_id: str | None,
        decision_id: str | None,
        now: int,
        client_order_id: str | None = None,
        status: str | None = None,
        symbol: str | None = None,
        side: str | None = None,
        order_type: str | None = None,
        amount_usdt: Any = None,
        filled_qty: Any = None,
        avg_price: Any = None,
        fee: Any = None,
        fee_currency: str | None = None,
        exchange_order_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """订单 upsert(必须已在事务内)。order 对象与显式参数二选一。"""
        if order is not None:
            client_order_id = getattr(order, "client_order_id", None) or client_order_id
            status = getattr(order, "status", None) or status
            symbol = getattr(order, "symbol", None) or symbol
            side = getattr(order, "side", None) or side
            order_type = getattr(order, "order_type", None) or order_type
            amount_usdt = getattr(order, "amount_usdt", amount_usdt)
            filled_qty = getattr(order, "filled_qty", None)
            avg_price = getattr(order, "avg_price", None)
            fee = getattr(order, "fee", None)
            fee_currency = getattr(order, "fee_currency", None)
            exchange_order_id = getattr(order, "exchange_order_id", None) or exchange_order_id
            error = getattr(order, "error", None) or error
        if not client_order_id or not status:
            return
        row = session.scalar(select(OrderRow).where(OrderRow.client_order_id == client_order_id))
        if row is None:
            row = OrderRow(client_order_id=client_order_id, created_at=now, recorded_at=now)
            session.add(row)
        row.status = status
        row.cycle_id = cycle_id or row.cycle_id
        row.decision_id = decision_id or row.decision_id
        row.symbol = symbol or row.symbol
        row.side = side or row.side
        row.order_type = order_type or row.order_type
        if amount_usdt is not None:
            row.amount_usdt = _to_amount(amount_usdt)
        if filled_qty is not None:
            row.filled_qty = _to_amount(filled_qty)
        if avg_price is not None:
            row.avg_price = _to_amount(avg_price)
        if fee is not None:
            row.fee = _to_amount(fee)
        if fee_currency is not None:
            row.fee_currency = fee_currency
        if exchange_order_id is not None:
            row.exchange_order_id = exchange_order_id
        row.error = error
        row.updated_at = now


# ---------------------------------------------------------------------- 辅助


def _trade_from_order(order: Any, *, decision_id: str | None, now: int) -> TradeRow | None:
    """已成交/部分成交的订单合成一条成交记录(本地模拟粒度)。"""
    status = getattr(order, "status", None)
    filled = _to_amount(getattr(order, "filled_qty", None)) or 0.0
    avg = _to_amount(getattr(order, "avg_price", None))
    cid = getattr(order, "client_order_id", None)
    if status not in ("FILLED", "PARTIALLY_FILLED") or filled <= 0 or not cid:
        return None
    trade = TradeRow(
        client_order_id=cid,
        decision_id=decision_id,
        symbol=getattr(order, "symbol", "") or "",
        side=getattr(order, "side", "") or "",
        qty=filled,
        price=avg or 0.0,
        fee=_to_amount(getattr(order, "fee", None)),
        fee_currency=getattr(order, "fee_currency", None),
        executed_at=_int_ms(getattr(order, "updated_at", None)) or now,
        created_at=now,
    )
    # 稳定主键(同单同成交):重复 record_cycle 时重跑幂等、不报唯一冲突
    trade.id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"trade:{cid}"))
    return trade


def _extract_timeframes(market: Any) -> list[tuple[str, Any]]:
    """MarketSnapshot.series 是 {timeframe: OHLCVSeries}。"""
    series_map = getattr(market, "series", None)
    if isinstance(series_map, dict):
        return list(series_map.items())
    return []


def _tail_closes(series: Any, n: int = 30) -> list[float]:
    candles = getattr(series, "candles", None) or []
    out = []
    for c in candles[-n:]:
        close = getattr(c, "close", None)
        if close is not None:
            out.append(float(close))
    return out


def _tf_source(series: Any) -> str | None:
    return getattr(series, "source", None)


def _tf_candle_count(series: Any) -> int | None:
    candles = getattr(series, "candles", None)
    return len(candles) if candles is not None else None


def _tf_open_time(series: Any) -> int | None:
    candles = getattr(series, "candles", None) or []
    return _int_ms(getattr(candles[0], "open_time", None)) if candles else None


def _tf_close_time(series: Any) -> int | None:
    candles = getattr(series, "candles", None) or []
    return _int_ms(getattr(candles[-1], "close_time", None)) if candles else None


def _tf_fetched_at(series: Any, default: int) -> int | None:
    return _int_ms(getattr(series, "fetched_at", None)) or default


_IND_FIELDS = (
    "last_close",
    "ma20",
    "ma60",
    "rsi14",
    "macd_dif",
    "macd_dea",
    "macd_hist",
    "atr14",
    "bb_mid",
    "bb_upper",
    "bb_lower",
    "volume_ma20",
    "volume_roc1",
)


def _indicator_fields(snap: Any) -> dict[str, float | None]:
    """IndicatorSnapshot → 行字段(None 保留)。"""
    return {key: _to_amount(getattr(snap, key, None)) for key in _IND_FIELDS}


# ------------------------------------------------------------------ 构建


def build_recorder(db_url: str) -> Recorder | None:
    """按配置构建 Recorder;URL 为空返回 None(未启用数据库)。"""
    if not db_url:
        return None
    engine = make_engine(db_url)
    apply_migrations(engine)
    _log.info("数据库已启用(schema 就绪)")
    return Recorder(engine)
