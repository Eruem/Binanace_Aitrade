"""阶段 6:Binance Testnet Broker 测试(指导书 9.1/13)。

下单精度、最小名义预检(不达标的单绝不发往交易所)、状态 1:1 映射、
交易所拒绝 → REJECTED、网络超时 → 上抛(不盲目重发)。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from exchange.binance_client import ExchangeAPIError, ExchangeNetworkError
from exchange.interfaces import BrokerError
from exchange.order_state import OrderStatus
from exchange.testnet_broker import BinanceTestnetBroker, quantize_step
from risk.order_validator import OrderRequest

D = Decimal
NOW = 1_793_145_600_000
SYMBOL = "BTC/USDT"
CID = "ai-cycle-test-0001"


def buy_request(amount: str = "500") -> OrderRequest:
    return OrderRequest(
        decision_id="cycle-test-0001", symbol=SYMBOL, side="BUY", amount_usdt=D(amount)
    )


def sell_request(amount: str = "500") -> OrderRequest:
    return OrderRequest(
        decision_id="cycle-test-0001", symbol=SYMBOL, side="SELL", amount_usdt=D(amount)
    )


def raw_order(**over) -> dict:
    base = dict(
        symbol="BTCUSDT",
        orderId=42,
        clientOrderId=CID,
        side="BUY",
        status="FILLED",
        origQty="0.00999",
        executedQty="0.00999",
        cummulativeQuoteQty="500.00",
        time=NOW - 500,
        updateTime=NOW,
    )
    base.update(over)
    return base


class FakeClient:
    """假 BinanceSpotClient(注入 BinanceTestnetBroker)。"""

    def __init__(self) -> None:
        self.buy_response: dict = raw_order()
        self.sell_response: dict = raw_order(side="SELL")
        self.order_response: dict | None = None
        self.cancel_response: dict = raw_order(status="CANCELED", executedQty="0")
        self.ticker_price: Decimal = D("50000")
        self.rules = None  # SymbolRules;None 时按默认构造
        self.account_balances: list[dict] = []
        self.trades: list[dict] = []
        self.buy_calls: list[dict] = []
        self.sell_calls: list[dict] = []
        self.buy_error: Exception | None = None
        self.sell_error: Exception | None = None
        self.order_error: Exception | None = None

    async def fetch_symbol_rules(self, symbol: str):
        if self.rules is not None:
            return self.rules
        from exchange.binance_client import SymbolRules

        return SymbolRules(
            symbol=symbol, amount_step=D("0.00001000"), min_notional=D("5.0000")
        )

    async def create_market_buy_quote(self, symbol, *, quote_qty, client_order_id):
        if self.buy_error is not None:
            raise self.buy_error
        self.buy_calls.append(
            {"symbol": symbol, "quote_qty": quote_qty, "client_order_id": client_order_id}
        )
        return dict(self.buy_response)

    async def create_market_sell(self, symbol, *, quantity, client_order_id):
        if self.sell_error is not None:
            raise self.sell_error
        self.sell_calls.append(
            {"symbol": symbol, "quantity": quantity, "client_order_id": client_order_id}
        )
        return dict(self.sell_response)

    async def fetch_order(self, client_order_id, *, symbol):
        if self.order_error is not None:
            raise self.order_error
        if self.order_response is None:
            return None
        return dict(self.order_response)

    async def cancel_order(self, client_order_id, *, symbol):
        return dict(self.cancel_response)

    async def fetch_order_trades(self, symbol, order_id):
        return list(self.trades)

    async def fetch_ticker_price(self, symbol):
        return self.ticker_price

    async def fetch_account_balances(self):
        return list(self.account_balances)


def make_broker(client: FakeClient | None = None) -> tuple[BinanceTestnetBroker, FakeClient]:
    c = client or FakeClient()
    return BinanceTestnetBroker(c, now_ms=lambda: NOW), c


# ----------------------------------------------------------------------
# 数量工具
# ----------------------------------------------------------------------
def test_quantize_step_down():
    assert quantize_step(D("0.01234567"), D("0.00001")) == D("0.01234")
    assert quantize_step(D("1"), D("0.5")) == D("1.0")
    assert quantize_step(D("0.9999"), D("1")) == D("0")


def test_quantize_step_rejects_bad_step():
    with pytest.raises(BrokerError):
        quantize_step(D("1"), D("0"))


# ----------------------------------------------------------------------
# BUY:quoteOrderQty 精确花费
# ----------------------------------------------------------------------
async def test_buy_sends_quote_order_qty():
    broker, c = make_broker()
    result = await broker.submit_order(buy_request("500.123456789"), client_order_id=CID)
    assert len(c.buy_calls) == 1
    call = c.buy_calls[0]
    # quoteOrderQty:8 位向下取整(与 LOT_SIZE 步长无关)
    assert call["quote_qty"] == D("500.12345678")
    assert call["client_order_id"] == CID
    assert result.status == OrderStatus.FILLED
    assert result.filled_qty == D("0.00999")
    # 均价 = cummulativeQuoteQty / executedQty
    assert result.avg_price == D("500.00") / D("0.00999")
    assert result.amount_usdt == D("500.00")
    assert result.symbol == SYMBOL
    assert result.exchange_order_id == "42"


async def test_buy_below_min_notional_rejected_locally():
    """低于最小名义 → 本地 REJECTED,绝不发往交易所(零下单调用)。"""
    broker, c = make_broker()
    result = await broker.submit_order(buy_request("4.99"), client_order_id=CID)
    assert result.status == OrderStatus.REJECTED
    assert "最小名义" in (result.error or "")
    assert c.buy_calls == []  # 没有产生任何真实下单
    assert c.sell_calls == []


# ----------------------------------------------------------------------
# SELL:ticker 估算数量 + 步长取整
# ----------------------------------------------------------------------
async def test_sell_quantizes_by_step_and_ticker():
    broker, c = make_broker()
    c.sell_response = raw_order(side="SELL", origQty="0.01000", executedQty="0.01000")
    result = await broker.submit_order(sell_request("500"), client_order_id=CID)
    assert len(c.sell_calls) == 1
    # 500 / 50000 = 0.01;按 step 0.00001 取整 → 0.01000
    assert c.sell_calls[0]["quantity"] == D("0.01000")
    assert result.side == "SELL"


async def test_sell_amount_too_small_rejected():
    """折算数量为 0 → 本地 REJECTED,不发单。"""
    broker, c = make_broker()
    c.ticker_price = D("50000")
    result = await broker.submit_order(sell_request("0.0001"), client_order_id=CID)
    assert result.status == OrderStatus.REJECTED
    assert c.sell_calls == []


async def test_sell_below_min_notional_rejected():
    """数量合法但名义金额低于交易所下限 → 本地 REJECTED,不发单。"""
    from exchange.binance_client import SymbolRules

    broker, c = make_broker()
    c.rules = SymbolRules(symbol=SYMBOL, amount_step=D("1"), min_notional=D("100"))
    c.ticker_price = D("50000")
    # 5 / 50000 = 0.0001 → step=1 取整为 0 → 数量 0 分支;换 step 0.0001 名义 5 < 100
    c.rules = SymbolRules(symbol=SYMBOL, amount_step=D("0.0001"), min_notional=D("100"))
    result = await broker.submit_order(sell_request("5"), client_order_id=CID)
    assert result.status == OrderStatus.REJECTED
    assert "最小名义" in (result.error or "")
    assert c.sell_calls == []


# ----------------------------------------------------------------------
# 状态映射与费用
# ----------------------------------------------------------------------
async def test_status_mapping_one_to_one():
    """Binance 原生状态 → 订单状态机 1:1 映射。"""
    broker, c = make_broker()
    for native, mapped in [
        ("NEW", OrderStatus.NEW),
        ("PARTIALLY_FILLED", OrderStatus.PARTIALLY_FILLED),
        ("FILLED", OrderStatus.FILLED),
        ("CANCELED", OrderStatus.CANCELED),
        ("REJECTED", OrderStatus.REJECTED),
        ("EXPIRED", OrderStatus.EXPIRED),
    ]:
        c.buy_response = raw_order(status=native)
        result = await broker.submit_order(buy_request("500"), client_order_id=CID)
        assert result.status == mapped, native


async def test_unknown_native_status_maps_unknown():
    broker, c = make_broker()
    c.buy_response = raw_order(status="SOME_FUTURE_STATUS")
    result = await broker.submit_order(buy_request("500"), client_order_id=CID)
    assert result.status == OrderStatus.UNKNOWN


async def test_api_rejection_becomes_rejected():
    """交易所明确拒绝 → REJECTED(携带原因);不是 UNKNOWN。"""
    broker, c = make_broker()
    c.buy_error = ExchangeAPIError("Insufficient balance")
    result = await broker.submit_order(buy_request("500"), client_order_id=CID)
    assert result.status == OrderStatus.REJECTED
    assert "Insufficient balance" in (result.error or "")


async def test_network_error_propagates_unknown():
    """网络超时 → 异常上抛(OrderManager 标记 UNKNOWN;绝不当作"订单不存在")。"""
    broker, c = make_broker()
    c.buy_error = ExchangeNetworkError("RequestTimeout: timed out")
    with pytest.raises(ExchangeNetworkError):
        await broker.submit_order(buy_request("500"), client_order_id=CID)


async def test_fee_from_mytrades_quote_asset_only():
    """手续费汇总仅计价货币(USDT)部分;其他资产费用不计入(见已知限制)。"""
    broker, c = make_broker()
    c.trades = [
        {"commission": "0.25", "commissionAsset": "USDT"},
        {"commission": "0.00001", "commissionAsset": "BNB"},  # 忽略
        {"commission": "0.15", "commissionAsset": "USDT"},
    ]
    result = await broker.submit_order(buy_request("500"), client_order_id=CID)
    assert result.fee == D("0.40")
    assert result.fee_currency == "USDT"


async def test_partial_fill_zero_quote_falls_back_to_request():
    """市价单未回填 cummulativeQuoteQty 时用请求金额兜底。"""
    broker, c = make_broker()
    c.buy_response = raw_order(
        status="PARTIALLY_FILLED",
        executedQty="0.005",
        origQty="0.01",
        cummulativeQuoteQty="0",
    )
    result = await broker.submit_order(buy_request("500"), client_order_id=CID)
    assert result.status == OrderStatus.PARTIALLY_FILLED
    assert result.amount_usdt == D("500")
    assert result.avg_price == D("500") / D("0.005")


# ----------------------------------------------------------------------
# 查询 / 撤单 / 账户
# ----------------------------------------------------------------------
async def test_get_order_requires_symbol():
    broker, _ = make_broker()
    with pytest.raises(BrokerError):
        await broker.get_order(CID)  # type: ignore[arg-type]


async def test_get_order_none_when_absent():
    broker, _ = make_broker()
    assert await broker.get_order(CID, symbol=SYMBOL) is None


async def test_get_order_returns_result():
    broker, c = make_broker()
    c.order_response = raw_order(status="NEW", executedQty="0", cummulativeQuoteQty="0")
    result = await broker.get_order(CID, symbol=SYMBOL)
    assert result is not None
    assert result.status == OrderStatus.NEW
    assert result.client_order_id == CID


async def test_cancel_terminal_order_idempotent():
    """终态订单撤单:幂等返回原结果,不调用交易所撤单接口。"""
    broker, c = make_broker()
    c.order_response = raw_order(status="FILLED")
    calls_before = len(c.sell_calls)
    result = await broker.cancel_order(CID, symbol=SYMBOL)
    assert result.status == OrderStatus.FILLED
    # cancel 未被调用:FakeClient.cancel_order 只会返回 CANCELED


async def test_cancel_requires_symbol():
    broker, _ = make_broker()
    with pytest.raises(BrokerError):
        await broker.cancel_order(CID)  # type: ignore[arg-type]


async def test_get_account_marks_positions_at_ticker():
    broker, c = make_broker()
    c.account_balances = [
        {"asset": "USDT", "free": D("900"), "locked": D("100")},
        {"asset": "BTC", "free": D("0.01"), "locked": D("0")},
    ]
    c.ticker_price = D("50000")
    snap = await broker.get_account()
    # equity = 900 + 100 + 0.01*50000 = 1500
    assert snap.equity_usdt == D("1500")
    assert snap.available_usdt == D("900")
    assert snap.open_orders_locked_usdt == D("100")
    assert snap.open_exposure_usdt == D("500")
    assert len(snap.positions) == 1
    assert snap.positions[0].symbol == "BTC/USDT"
    assert snap.positions[0].qty == D("0.01")


async def test_get_account_skips_asset_without_market():
    """无现货市场资产(BadSymbol)跳过,不废掉整个账户快照。

    回归背景:Live 账户持有理财收据资产 LDBTTC(X/USDT 不存在),
    get_account 标记行情抛 BadSymbol → 快照失败 → 风控永久否决一切。
    """
    broker, c = make_broker()
    c.account_balances = [
        {"asset": "USDT", "free": D("100"), "locked": D("0")},
        {"asset": "LDBTTC", "free": D("6.4"), "locked": D("0")},
        {"asset": "BTC", "free": D("0.01"), "locked": D("0")},
    ]
    orig = c.fetch_ticker_price

    async def fake_ticker(symbol):
        if symbol == "LDBTTC/USDT":
            raise ExchangeAPIError(
                'BadSymbol: binance {"code":-1121,"msg":"Invalid symbol."}'
            )
        return await orig(symbol)

    c.fetch_ticker_price = fake_ticker  # type: ignore[assignment]
    snap = await broker.get_account()
    # LDBTTC 被跳过;BTC/USDT 正常标记;快照不失败
    assert [p.symbol for p in snap.positions] == ["BTC/USDT"]
    assert snap.open_exposure_usdt == D("500")  # 0.01 * 50000
    assert snap.equity_usdt == D("600")  # 100 + 500


async def test_get_account_ticker_network_error_still_fails_safe():
    """非 BadSymbol 的行情失败(断线)照常上抛 → 风控 fail-safe 否决。"""
    broker, c = make_broker()
    c.account_balances = [{"asset": "BTC", "free": D("0.01"), "locked": D("0")}]

    async def boom(symbol):
        raise ExchangeNetworkError("timeout")

    c.fetch_ticker_price = boom  # type: ignore[assignment]
    with pytest.raises(ExchangeNetworkError):
        await broker.get_account()


async def test_get_account_non_bad_symbol_api_error_propagates():
    """交易所其他明确拒绝(非 BadSymbol)同样上抛,不被跳过逻辑吞掉。"""
    broker, c = make_broker()
    c.account_balances = [{"asset": "BTC", "free": D("0.01"), "locked": D("0")}]

    async def boom(symbol):
        raise ExchangeAPIError("Insufficient balance for ticker")

    c.fetch_ticker_price = boom  # type: ignore[assignment]
    with pytest.raises(ExchangeAPIError):
        await broker.get_account()
