"""Paper Broker(阶段 5,指导书 9.1/13)。

本地模拟成交,不连接任何交易所:
- 价格:注入的 price_provider(或 OrderRequest.price_hint);
- 滑点:BUY 成交价 = 参考价 ×(1+slippage),SELL = 参考价 ×(1-slippage);
- 手续费:按成交名义金额计费,默认计价货币 USDT;
  BUY 手续费计入持仓成本基础,SELL 手续费从收入中扣除;
- 部分成交:fill_ratio < 1 时首次只成交一部分(状态 PARTIALLY_FILLED),
  complete_fill() 按原价补足剩余(状态 FILLED);
- 幂等:同一 client_order_id 重复提交返回同一订单,绝不重复记账;
- 持久化:订单/成交/余额原子写入本地 JSON(var/ 已在 .gitignore);
  状态文件损坏 → 构造时即抛错,拒绝交易(无法保证幂等就不下单)。

安全边界:本模块不持有、不读取任何 API Key。
"""

from __future__ import annotations

import json
import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Callable

from risk.daily_loss import utc_day
from risk.engine import AccountSnapshot, Position
from risk.order_validator import OrderRequest

from .interfaces import (
    Broker,
    BrokerError,
    OrderResult,
    PaperBrokerStateError,
    TradeRecord,
)
from .order_state import OrderStatus, assert_transition

QTY_STEP = Decimal("0.00000001")  # 基础资产数量精度(8 位小数,向下取整)
FEE_STEP = Decimal("0.00000001")

_REQUIRED_KEYS = ("seq", "balances", "avg_cost", "orders", "trades", "realized")


class PaperBroker(Broker):
    """本地模拟 Broker(默认模式;绝不触网、绝不用密钥)。"""

    name = "paper_broker"

    def __init__(
        self,
        *,
        price_provider: Callable[[str], Decimal] | None = None,
        state_path: str | Path | None = None,
        fee_rate: Decimal | str | float = "0.001",
        slippage_rate: Decimal | str | float = "0.0005",
        fill_ratio: Decimal | str | float = "1",
        initial_balances: dict[str, Decimal | str] | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._price_provider = price_provider
        self._path = Path(state_path) if state_path else None
        self.fee_rate = Decimal(str(fee_rate))
        self.slippage_rate = Decimal(str(slippage_rate))
        self.fill_ratio = Decimal(str(fill_ratio))
        if not (Decimal("0") < self.fill_ratio <= Decimal("1")):
            raise ValueError("fill_ratio 必须在 (0, 1] 区间")
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._initial_balances: dict[str, Decimal] = {
            "USDT": Decimal("10000"),
        }
        if initial_balances:
            for k, v in initial_balances.items():
                self._initial_balances[k] = Decimal(str(v))
        self._mem_state: dict | None = None  # 无 state_path 时的内存状态
        # 状态文件存在即立刻校验:损坏在构造时就拒绝,不留半初始化状态
        if self._path and self._path.exists():
            self._load()

    # ------------------------------------------------------------------
    # 持久化(原子写;损坏即抛错,拒绝交易)
    # ------------------------------------------------------------------
    def _blank_state(self) -> dict:
        return {
            "seq": 0,
            "balances": {k: str(v) for k, v in self._initial_balances.items()},
            "avg_cost": {},  # {base: Decimal 成本基础单价(含买费)}
            "orders": {},  # {client_order_id: record}
            "trades": [],  # [record]
            "realized": {},  # {utc_day: str} 已实现盈亏(卖费已扣)
        }

    def _load(self) -> dict:
        if self._path is None:
            # 内存模式:同一 dict 对象贯穿整个生命周期(否则每次读都是空的)
            if self._mem_state is None:
                self._mem_state = self._blank_state()
            return self._mem_state
        if not self._path.exists():
            return self._blank_state()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or any(k not in raw for k in _REQUIRED_KEYS):
                raise ValueError("状态文件结构不完整")
            # 触发一次解码校验:坏 Decimal 立刻抛
            for v in raw["balances"].values():
                Decimal(v)
            return raw
        except (OSError, ValueError, TypeError) as e:
            raise PaperBrokerStateError(
                f"Paper Broker 状态文件损坏,拒绝交易(无法保证幂等): {e}"
            ) from e

    def _save(self, state: dict) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, self._path)  # 原子替换,避免半写状态

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _dec(self, rec: dict, key: str) -> Decimal | None:
        v = rec.get(key)
        return None if v is None else Decimal(v)

    def _result(self, rec: dict) -> OrderResult:
        return OrderResult(
            client_order_id=rec["client_order_id"],
            exchange_order_id=rec.get("exchange_order_id"),
            decision_id=rec["decision_id"],
            symbol=rec["symbol"],
            side=rec["side"],
            status=OrderStatus(rec["status"]),
            amount_usdt=Decimal(rec["amount_usdt"]),
            requested_qty=Decimal(rec["requested_qty"]),
            filled_qty=Decimal(rec["filled_qty"]),
            avg_price=self._dec(rec, "avg_price"),
            fee=Decimal(rec["fee"]),
            fee_currency=rec.get("fee_currency", "USDT"),
            created_at=rec["created_at"],
            updated_at=rec["updated_at"],
            error=rec.get("error"),
        )

    def _append_trade(self, state: dict, trade: dict) -> None:
        state["trades"].append(trade)

    def _apply_fill(
        self,
        state: dict,
        rec: dict,
        *,
        qty: Decimal,
        price: Decimal,
        now: int,
    ) -> None:
        """按给定数量/价格结算一次成交,更新余额、成本基础、盈亏与成交记录。"""
        side = rec["side"]
        base = rec["symbol"].split("/")[0]
        quote = rec["symbol"].split("/")[1]
        fee = (qty * price * self.fee_rate).quantize(FEE_STEP, ROUND_HALF_UP)

        balances = state["balances"]
        avg_cost = state["avg_cost"]
        if side == "BUY":
            cost = qty * price
            balances[quote] = str(Decimal(balances.get(quote, "0")) - cost - fee)
            new_base = Decimal(balances.get(base, "0")) + qty
            old_base = new_base - qty
            old_avg = Decimal(avg_cost.get(base, "0"))
            # 买费计入成本基础:avg = (旧成本 + 本次成交 + 本次费) / 新数量
            total_cost = old_base * old_avg + cost + fee
            avg_cost[base] = str(
                (total_cost / new_base) if new_base > 0 else Decimal("0")
            )
            balances[base] = str(new_base)
        else:  # SELL:卖费从收入中扣;已实现盈亏按成本基础计算
            balances[quote] = str(
                Decimal(balances.get(quote, "0")) + qty * price - fee
            )
            new_base = Decimal(balances.get(base, "0")) - qty
            old_avg = Decimal(avg_cost.get(base, "0"))
            realized = (price - old_avg) * qty - fee
            day = utc_day(now)
            state["realized"][day] = str(
                Decimal(state["realized"].get(day, "0")) + realized
            )
            balances[base] = str(new_base)
            if new_base <= 0:
                avg_cost.pop(base, None)

        # 订单内累计:加权平均成交价
        prev_filled = Decimal(rec["filled_qty"])
        filled = prev_filled + qty
        prev_avg = Decimal(rec["avg_price"]) if rec.get("avg_price") else Decimal("0")
        rec["filled_qty"] = str(filled)
        rec["fee"] = str(Decimal(rec["fee"]) + fee)
        rec["avg_price"] = str((prev_avg * prev_filled + qty * price) / filled)
        rec["updated_at"] = now

        self._append_trade(
            state,
            {
                "trade_id": f"{rec.get('exchange_order_id') or rec['client_order_id']}-t{len(state['trades']) + 1}",
                "client_order_id": rec["client_order_id"],
                "exchange_order_id": rec.get("exchange_order_id"),
                "decision_id": rec["decision_id"],
                "symbol": rec["symbol"],
                "side": side,
                "qty": str(qty),
                "price": str(price),
                "fee": str(fee),
                "fee_currency": quote,
                "executed_at": now,
            },
        )

    def _set_status(self, rec: dict, status: OrderStatus, now: int) -> None:
        assert_transition(OrderStatus(rec["status"]), status)
        rec["status"] = status.value
        rec["history"].append(status.value)
        rec["updated_at"] = now

    def _reject(
        self,
        state: dict,
        request: OrderRequest,
        client_order_id: str,
        reason: str,
        now: int,
    ) -> OrderResult:
        rec = {
            "client_order_id": client_order_id,
            "exchange_order_id": None,  # 从未到达交易所
            "decision_id": request.decision_id,
            "symbol": request.symbol,
            "side": request.side,
            "status": OrderStatus.REJECTED.value,
            "history": [OrderStatus.REJECTED.value],
            "amount_usdt": str(request.amount_usdt),
            "requested_qty": "0",
            "filled_qty": "0",
            "avg_price": None,
            "fee": "0",
            "fee_currency": "USDT",
            "fill_price": None,
            "created_at": now,
            "updated_at": now,
            "error": reason,
        }
        state["orders"][client_order_id] = rec
        self._save(state)
        return self._result(rec)

    # ------------------------------------------------------------------
    # Broker 接口
    # ------------------------------------------------------------------
    async def submit_order(
        self, request: OrderRequest, *, client_order_id: str
    ) -> OrderResult:
        now = self._now()
        state = self._load()

        # 幂等:同一 client_order_id 直接返回已有订单,绝不重复记账
        existing = state["orders"].get(client_order_id)
        if existing is not None:
            return self._result(existing)

        if request.side not in ("BUY", "SELL"):
            return self._reject(
                state, request, client_order_id, f"非法方向 {request.side!r}", now
            )
        if request.amount_usdt <= 0:
            return self._reject(state, request, client_order_id, "金额必须为正", now)
        if "/" not in request.symbol:
            return self._reject(
                state, request, client_order_id, f"非法交易对 {request.symbol}", now
            )

        # 参考价:优先实时 provider,回退 price_hint;都没有 → 拒绝(不猜价)
        ref: Decimal | None = None
        if self._price_provider is not None:
            try:
                ref = Decimal(str(self._price_provider(request.symbol)))
            except Exception:
                ref = None
        if ref is None and request.price_hint is not None:
            ref = Decimal(str(request.price_hint))
        if ref is None or ref <= 0:
            return self._reject(
                state, request, client_order_id, "无法获取参考价格,拒绝下单", now
            )

        # 滑点:BUY 成交价更高,SELL 成交价更低
        if request.side == "BUY":
            fill_price = ref * (Decimal("1") + self.slippage_rate)
        else:
            fill_price = ref * (Decimal("1") - self.slippage_rate)

        qty = (request.amount_usdt / fill_price).quantize(QTY_STEP, ROUND_DOWN)
        if qty <= 0:
            return self._reject(
                state, request, client_order_id, "折算数量为 0(金额过小)", now
            )

        base, quote = request.symbol.split("/", 1)
        balances = state["balances"]
        if request.side == "BUY":
            est_cost = qty * fill_price
            est_fee = (est_cost * self.fee_rate).quantize(FEE_STEP, ROUND_HALF_UP)
            if est_cost + est_fee > Decimal(balances.get(quote, "0")):
                return self._reject(
                    state, request, client_order_id,
                    f"{quote} 余额不足:需要 {est_cost + est_fee},可用 {balances.get(quote, '0')}",
                    now,
                )
        else:
            if qty > Decimal(balances.get(base, "0")):
                return self._reject(
                    state, request, client_order_id,
                    f"持仓不足:卖出 {qty},持有 {balances.get(base, '0')}",
                    now,
                )

        state["seq"] = int(state["seq"]) + 1
        rec = {
            "client_order_id": client_order_id,
            "exchange_order_id": f"paper-{state['seq']}",
            "decision_id": request.decision_id,
            "symbol": request.symbol,
            "side": request.side,
            "status": OrderStatus.NEW.value,
            "history": [OrderStatus.NEW.value],
            "amount_usdt": str(request.amount_usdt),
            "requested_qty": str(qty),
            "filled_qty": "0",
            "avg_price": None,
            "fee": "0",
            "fee_currency": quote,
            "fill_price": str(fill_price),
            "created_at": now,
            "updated_at": now,
            "error": None,
        }
        state["orders"][client_order_id] = rec

        # 首次成交量:fill_ratio < 1 时只成交一部分(剩余可 complete_fill)
        if self.fill_ratio < Decimal("1"):
            first = (qty * self.fill_ratio).quantize(QTY_STEP, ROUND_DOWN)
            if first <= 0:
                first = qty  # 精度内无法拆分 → 全额成交
        else:
            first = qty
        remaining = qty - first

        self._apply_fill(state, rec, qty=first, price=fill_price, now=now)
        if remaining > 0:
            self._set_status(rec, OrderStatus.PARTIALLY_FILLED, now)
        else:
            self._set_status(rec, OrderStatus.FILLED, now)
        self._save(state)
        return self._result(rec)

    async def get_order(
        self, client_order_id: str, *, symbol: str | None = None
    ) -> OrderResult | None:
        # Paper 全部本地可查,忽略 symbol(真实交易所查询必须带交易对)
        state = self._load()
        rec = state["orders"].get(client_order_id)
        return None if rec is None else self._result(rec)

    async def complete_fill(self, client_order_id: str) -> OrderResult:
        """补足部分成交的剩余数量(按原成交价)。仅 PARTIALLY_FILLED 可调用。"""
        now = self._now()
        state = self._load()
        rec = state["orders"].get(client_order_id)
        if rec is None:
            raise BrokerError(f"订单不存在: {client_order_id}")
        if rec["status"] != OrderStatus.PARTIALLY_FILLED.value:
            raise BrokerError(
                f"订单状态 {rec['status']} 不支持补足成交(仅 PARTIALLY_FILLED)"
            )
        remaining = Decimal(rec["requested_qty"]) - Decimal(rec["filled_qty"])
        price = Decimal(rec["fill_price"])
        if remaining > 0:
            self._apply_fill(state, rec, qty=remaining, price=price, now=now)
        self._set_status(rec, OrderStatus.FILLED, now)
        self._save(state)
        return self._result(rec)

    async def cancel_order(
        self, client_order_id: str, *, symbol: str | None = None
    ) -> OrderResult:
        now = self._now()
        state = self._load()
        rec = state["orders"].get(client_order_id)
        if rec is None:
            raise BrokerError(f"订单不存在: {client_order_id}")
        self._set_status(rec, OrderStatus.CANCELED, now)
        self._save(state)
        return self._result(rec)

    def trades(self, client_order_id: str | None = None) -> list[TradeRecord]:
        """成交记录(可按订单过滤)。"""
        state = self._load()
        out = []
        for t in state["trades"]:
            if client_order_id is not None and t["client_order_id"] != client_order_id:
                continue
            out.append(
                TradeRecord(
                    trade_id=t["trade_id"],
                    client_order_id=t["client_order_id"],
                    exchange_order_id=t.get("exchange_order_id"),
                    decision_id=t["decision_id"],
                    symbol=t["symbol"],
                    side=t["side"],
                    qty=Decimal(t["qty"]),
                    price=Decimal(t["price"]),
                    fee=Decimal(t["fee"]),
                    fee_currency=t.get("fee_currency", "USDT"),
                    executed_at=t["executed_at"],
                )
            )
        return out

    # ------------------------------------------------------------------
    # 账户快照(供风控;与 risk.engine.AccountSnapshot 对齐)
    # ------------------------------------------------------------------
    def _mark_price(self, symbol: str, fallback: Decimal) -> Decimal:
        if self._price_provider is None:
            return fallback
        try:
            return Decimal(str(self._price_provider(symbol)))
        except Exception:
            return fallback

    async def get_account(self) -> AccountSnapshot:
        state = self._load()
        balances = {k: Decimal(v) for k, v in state["balances"].items()}
        quote = "USDT"
        now = self._now()

        # 未成交部分锁定:PARTIALLY_FILLED 的 BUY 剩余量按成交价+费预估
        locked = Decimal("0")
        for rec in state["orders"].values():
            if rec["status"] != OrderStatus.PARTIALLY_FILLED.value:
                continue
            if rec["side"] != "BUY":
                continue
            remaining = Decimal(rec["requested_qty"]) - Decimal(rec["filled_qty"])
            price = Decimal(rec["fill_price"])
            est = remaining * price * (Decimal("1") + self.fee_rate)
            locked += est

        positions: list[Position] = []
        exposure = Decimal("0")
        for base, qty in balances.items():
            if base == quote or qty <= 0:
                continue
            symbol = f"{base}/{quote}"
            avg = Decimal(state["avg_cost"].get(base, "0"))
            mark = self._mark_price(symbol, avg)
            positions.append(Position(symbol=symbol, qty=qty, entry_price=avg))
            exposure += qty * mark

        usdt = balances.get(quote, Decimal("0"))
        equity = usdt + exposure
        realized_today = Decimal(state["realized"].get(utc_day(now), "0"))
        return AccountSnapshot(
            equity_usdt=equity,
            available_usdt=usdt - locked,
            open_exposure_usdt=exposure,
            open_orders_locked_usdt=locked,
            positions=tuple(positions),
            daily_realized_pnl_usdt=realized_today,
            consecutive_losses=0,  # 连续亏损由策略层统计(阶段 7 接入)
            fetched_at=now,
        )
