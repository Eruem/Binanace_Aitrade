"""Binance Spot Testnet Broker(阶段 6,指导书 9.1/9.4)。

实现 Broker 接口,经 BinanceSpotClient 连接 Spot Testnet:
- BUY 用 quoteOrderQty(精确花费 amount_usdt);SELL 用 ticker 估算数量;
- 数量按 LOT_SIZE stepSize 向下取整;低于最小名义 → 本地拒绝(不下单);
- Binance 原生状态(NEW/PARTIALLY_FILLED/FILLED/CANCELED/REJECTED/EXPIRED)
  与订单状态机 1:1 映射;未知状态 → UNKNOWN;
- 交易所明确拒绝(ExchangeAPIError)→ REJECTED;
  网络超时/断线(ExchangeNetworkError)→ 抛出,由 OrderManager 标记
  UNKNOWN(绝不把网络失败当作"订单不存在");
- 手续费从 my_trades 汇总(仅计价货币部分;其余资产费用见已知限制)。

安全边界:不持有配置、不读环境变量;密钥只在注入的 Client 内部。
"""

from __future__ import annotations

import time
from decimal import Decimal, ROUND_DOWN
from typing import Callable

from risk.engine import AccountSnapshot, Position
from risk.order_validator import OrderRequest

from .binance_client import (
    BinanceSpotClient,
    ExchangeAPIError,
    ExchangeNetworkError,
)
from .interfaces import Broker, BrokerError, OrderResult
from .order_state import OrderStatus

# Binance 原生状态 → 订单状态机
_BINANCE_STATUS = {
    "NEW": OrderStatus.NEW,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
}

QUOTE_ASSET = "USDT"


def quantize_step(amount: Decimal, step: Decimal) -> Decimal:
    """按步长向下取整(交易所数量规则)。"""
    if step <= 0:
        raise BrokerError(f"非法 stepSize: {step}")
    return (amount / step).to_integral_value(rounding=ROUND_DOWN) * step


class BinanceTestnetBroker(Broker):
    """Testnet Spot Broker;订单查询与对账的真实数据源。"""

    name = "binance_testnet_broker"

    def __init__(
        self, client: BinanceSpotClient, *, now_ms: Callable[[], int] | None = None
    ) -> None:
        self._client = client
        self._now = now_ms or (lambda: int(time.time() * 1000))

    async def aclose(self) -> None:
        """关闭底层客户端(若支持);不影响任何本地/交易所状态。"""
        aclose = getattr(self._client, "aclose", None)
        if callable(aclose):
            await aclose()

    # ------------------------------------------------------------------
    # Binance 原始订单 → OrderResult
    # ------------------------------------------------------------------
    async def _to_result(
        self, raw: dict, *, request: OrderRequest | None, client_order_id: str
    ) -> OrderResult:
        status = _BINANCE_STATUS.get(str(raw.get("status", "")).upper(), OrderStatus.UNKNOWN)
        symbol = _slash_symbol(str(raw.get("symbol", "")))
        side = str(raw.get("side", request.side if request else "BUY")).upper()
        filled = Decimal(str(raw.get("executedQty") or "0"))
        requested_qty = Decimal(str(raw.get("origQty") or "0"))
        quote_spent = Decimal(str(raw.get("cummulativeQuoteQty") or "0"))
        if quote_spent <= 0 and request is not None:
            quote_spent = request.amount_usdt  # 市价单未回填时用请求金额
        avg_price: Decimal | None = None
        if filled > 0 and quote_spent > 0:
            avg_price = quote_spent / filled
        order_id = str(raw.get("orderId", ""))
        fee, fee_currency = await self._fee_for(symbol, order_id, filled)
        created = int(raw.get("time") or raw.get("transactTime") or self._now())
        updated = int(raw.get("updateTime") or created)
        client_id = str(raw.get("clientOrderId") or client_order_id)
        return OrderResult(
            client_order_id=client_id,
            exchange_order_id=order_id or None,
            decision_id=request.decision_id if request else client_order_id,
            symbol=symbol or (request.symbol if request else ""),
            side=side,  # type: ignore[arg-type]
            status=status,
            amount_usdt=quote_spent,
            requested_qty=requested_qty,
            filled_qty=filled,
            avg_price=avg_price,
            fee=fee,
            fee_currency=fee_currency,
            created_at=created,
            updated_at=updated,
            error=None,
        )

    async def _fee_for(
        self, symbol: str, order_id: str, filled: Decimal
    ) -> tuple[Decimal, str]:
        """my_trades 汇总计价货币手续费;查不到不致命(费用字段缺失)。"""
        if not order_id or filled <= 0:
            return Decimal("0"), QUOTE_ASSET
        try:
            trades = await self._client.fetch_order_trades(symbol, order_id)
        except ExchangeNetworkError:
            raise  # 网络问题必须上抛:不能悄悄丢状态
        except Exception:
            return Decimal("0"), QUOTE_ASSET
        fee = Decimal("0")
        for t in trades:
            if str(t.get("commissionAsset", "")) == QUOTE_ASSET:
                fee += Decimal(str(t.get("commission", "0")))
        return fee, QUOTE_ASSET

    def _rejected(
        self, request: OrderRequest, client_order_id: str, reason: str
    ) -> OrderResult:
        now = self._now()
        return OrderResult(
            client_order_id=client_order_id,
            exchange_order_id=None,
            decision_id=request.decision_id,
            symbol=request.symbol,
            side=request.side,  # type: ignore[arg-type]
            status=OrderStatus.REJECTED,
            amount_usdt=request.amount_usdt,
            requested_qty=Decimal("0"),
            filled_qty=Decimal("0"),
            avg_price=None,
            fee=Decimal("0"),
            fee_currency=QUOTE_ASSET,
            created_at=now,
            updated_at=now,
            error=reason,
        )

    # ------------------------------------------------------------------
    # Broker 接口
    # ------------------------------------------------------------------
    async def submit_order(
        self, request: OrderRequest, *, client_order_id: str
    ) -> OrderResult:
        if request.side not in ("BUY", "SELL"):
            return self._rejected(request, client_order_id, f"非法方向 {request.side!r}")
        rules = await self._client.fetch_symbol_rules(request.symbol)

        try:
            if request.side == "BUY":
                # quoteOrderQty 精度与 LOT_SIZE(基础资产)无关,统一 8 位向下取整
                quote_qty = request.amount_usdt.quantize(
                    Decimal("0.00000001"), rounding=ROUND_DOWN
                )
                if quote_qty < rules.min_notional:
                    return self._rejected(
                        request, client_order_id,
                        f"名义金额 {quote_qty} 低于交易所最小名义 {rules.min_notional}",
                    )
                raw = await self._client.create_market_buy_quote(
                    request.symbol, quote_qty=quote_qty, client_order_id=client_order_id
                )
            else:
                price = await self._client.fetch_ticker_price(request.symbol)
                qty = quantize_step(request.amount_usdt / price, rules.amount_step)
                if qty <= 0:
                    return self._rejected(
                        request, client_order_id, "折算数量为 0(金额过小)"
                    )
                if qty * price < rules.min_notional:
                    return self._rejected(
                        request, client_order_id,
                        f"名义金额 {qty * price} 低于交易所最小名义 {rules.min_notional}",
                    )
                raw = await self._client.create_market_sell(
                    request.symbol, quantity=qty, client_order_id=client_order_id
                )
        except ExchangeAPIError as e:
            # 交易所明确拒绝(余额不足/精度/风控)→ REJECTED,不是 UNKNOWN
            return self._rejected(request, client_order_id, f"交易所拒绝: {e}")
        # 网络类/未知异常不在此捕获:上抛 → OrderManager 标记 UNKNOWN

        return await self._to_result(raw, request=request, client_order_id=client_order_id)

    async def get_order(
        self, client_order_id: str, *, symbol: str | None = None
    ) -> OrderResult | None:
        if not symbol:
            raise BrokerError("Testnet 按 Client Order ID 查询必须提供 symbol")
        raw = await self._client.fetch_order(client_order_id, symbol=symbol)
        if raw is None:
            return None
        return await self._to_result(raw, request=None, client_order_id=client_order_id)

    async def cancel_order(
        self, client_order_id: str, *, symbol: str | None = None
    ) -> OrderResult:
        if not symbol:
            raise BrokerError("Testnet 撤单必须提供 symbol")
        existing = await self.get_order(client_order_id, symbol=symbol)
        if existing is not None and existing.status in (
            OrderStatus.FILLED, OrderStatus.CANCELED,
            OrderStatus.REJECTED, OrderStatus.EXPIRED,
        ):
            return existing  # 终态不可撤销,幂等返回
        raw = await self._client.cancel_order(client_order_id, symbol=symbol)
        return await self._to_result(raw, request=None, client_order_id=client_order_id)

    async def get_account(self) -> AccountSnapshot:
        """账户快照:USDT free 为可用;非 USDT 按 ticker 标记。"""
        balances = await self._client.fetch_account_balances()
        now = self._now()
        usdt_free = Decimal("0")
        usdt_locked = Decimal("0")
        positions: list[Position] = []
        exposure = Decimal("0")
        for b in balances:
            asset = b["asset"]
            total = b["free"] + b["locked"]
            if asset == QUOTE_ASSET:
                usdt_free, usdt_locked = b["free"], b["locked"]
                continue
            if total <= 0:
                continue
            symbol = f"{asset}/{QUOTE_ASSET}"
            try:
                mark = await self._client.fetch_ticker_price(symbol)
            except ExchangeAPIError as e:
                # 无现货市场的资产(理财收据如 LDBTTC、已下架币):无法标记
                # 且本身不可交易 → 跳过,不让单个资产废掉整个快照
                # (否则风控永久 fail-safe 否决一切);其余错误照常上抛。
                if "BadSymbol" in str(e) or "Invalid symbol" in str(e):
                    continue
                raise
            positions.append(
                Position(symbol=symbol, qty=total, entry_price=mark)
            )
            exposure += total * mark
        equity = usdt_free + usdt_locked + exposure
        return AccountSnapshot(
            equity_usdt=equity,
            available_usdt=usdt_free,  # 锁定部分是交易所托管,不可用
            open_exposure_usdt=exposure,
            open_orders_locked_usdt=usdt_locked,  # 账户级锁定(非订单级)
            positions=tuple(positions),
            daily_realized_pnl_usdt=Decimal("0"),  # 需成交明细汇总(阶段 7)
            consecutive_losses=0,
            fetched_at=now,
        )


def _slash_symbol(native: str) -> str:
    """BTCUSDT -> BTC/USDT(已带斜杠则原样)。"""
    if "/" in native:
        return native
    for quote in ("USDT", "USDC", "BTC", "ETH", "BNB"):
        if native.endswith(quote) and len(native) > len(quote):
            base = native[: -len(quote)]
            return f"{base}/{quote}"
    return native
