"""阶段 5:Paper Broker 测试(指导书 13 阶段 5 验收)。

全部离线:本地模拟成交,不发任何网络请求、不接触任何密钥。
手算基准(全部用干净数字):
    ref=50000, slippage=0.001, fee_rate=0.001
    BUY:  fill=50000×1.001=50050;amount=500.5 → qty=0.01
          cost=500.5 fee=0.5005 → USDT 花费 501.0005
          成本基础 avg=(500.5+0.5005)/0.01=50100.05
    SELL: fill=50000×0.999=49950;amount=499.5 → qty=0.01
          收入 499.5 fee=0.4995 → USDT 到账 499.0005
          已实现盈亏=(49950-50100.05)×0.01-0.4995=-2.00
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from exchange.interfaces import BrokerError, PaperBrokerStateError
from exchange.order_state import InvalidTransitionError, OrderStatus
from exchange.paper_broker import PaperBroker
from risk.order_validator import OrderRequest

D = Decimal
SYMBOL = "BTC/USDT"
REF = D("50000")


def make_broker(tmp_path, **over) -> PaperBroker:
    kw = dict(
        price_provider=lambda sym: REF,
        state_path=tmp_path / "paper.json",
        fee_rate="0.001",
        slippage_rate="0.001",
    )
    kw.update(over)
    return PaperBroker(**kw)


def buy_request(amount: str = "500.5", decision_id: str = "cycle-b1") -> OrderRequest:
    return OrderRequest(
        decision_id=decision_id, symbol=SYMBOL, side="BUY",
        amount_usdt=D(amount),
    )


def sell_request(amount: str = "499.5", decision_id: str = "cycle-s1") -> OrderRequest:
    return OrderRequest(
        decision_id=decision_id, symbol=SYMBOL, side="SELL",
        amount_usdt=D(amount),
    )


def state_json(tmp_path) -> dict:
    return json.loads((tmp_path / "paper.json").read_text(encoding="utf-8"))


async def test_buy_full_fill_math(tmp_path):
    """验收:完成一笔模拟 BUY(数量/费用/余额手算核对)。"""
    broker = make_broker(tmp_path)
    result = await broker.submit_order(buy_request(), client_order_id="ai-c1")
    assert result.status == OrderStatus.FILLED
    assert result.exchange_order_id == "paper-1"
    assert result.requested_qty == D("0.01")
    assert result.filled_qty == D("0.01")
    assert result.avg_price == D("50050")
    assert result.fee == D("0.5005")
    assert result.fee_currency == "USDT"

    account = await broker.get_account()
    # USDT: 10000 - 500.5 - 0.5005 = 9498.9995;BTC: 0.01
    assert account.available_usdt == D("9498.9995")
    assert account.positions[0].symbol == SYMBOL
    assert account.positions[0].qty == D("0.01")
    # 成本基础含手续费:501.0005 / 0.01 = 50100.05
    assert account.positions[0].entry_price == D("50100.05")

    trades = broker.trades("ai-c1")
    assert len(trades) == 1
    assert trades[0].qty == D("0.01")
    assert trades[0].price == D("50050")
    assert trades[0].fee == D("0.5005")


async def test_sell_full_fill_math(tmp_path):
    """验收:完成一笔模拟 SELL(先买后卖一轮;卖费从收入扣除)。"""
    broker = make_broker(tmp_path)
    await broker.submit_order(buy_request(), client_order_id="ai-b1")
    result = await broker.submit_order(sell_request(), client_order_id="ai-s1")
    assert result.status == OrderStatus.FILLED
    assert result.requested_qty == D("0.01")
    assert result.avg_price == D("49950")
    assert result.fee == D("0.4995")

    account = await broker.get_account()
    # USDT: 9498.9995 + 499.5 - 0.4995 = 9998.0000;BTC 清零
    assert account.available_usdt == D("9998.0000")
    assert account.positions == ()
    # 已实现盈亏(UTC 日键)=-2.00(Decimal 比较按数值,不受尾随零影响)
    realized = state_json(tmp_path)["realized"]
    assert [D(v) for v in realized.values()] == [D("-2.00")]


async def test_partial_fill_then_complete(tmp_path):
    """验收:支持部分成交模拟,补足后状态 FILLED、均价与总费正确。"""
    broker = make_broker(tmp_path, fill_ratio="0.4")
    result = await broker.submit_order(buy_request(), client_order_id="ai-p1")
    assert result.status == OrderStatus.PARTIALLY_FILLED
    assert result.requested_qty == D("0.01")
    assert result.filled_qty == D("0.004")
    assert result.avg_price == D("50050")

    # 部分成交后:剩余 BUY 锁定可用余额
    account = await broker.get_account()
    # 已花 0.004×50050×1.001 = 200.4002;锁定剩余 0.006×50050×1.001
    assert account.open_orders_locked_usdt == D("0.006") * D("50050") * D("1.001")
    assert account.available_usdt == D("10000") - D("200.4002") - account.open_orders_locked_usdt

    done = await broker.complete_fill("ai-p1")
    assert done.status == OrderStatus.FILLED
    assert done.filled_qty == D("0.01")
    assert done.fee == D("0.5005")  # 0.2002 + 0.3003
    assert done.avg_price == D("50050")
    assert len(broker.trades("ai-p1")) == 2
    account = await broker.get_account()
    assert account.open_orders_locked_usdt == D("0")
    assert account.available_usdt == D("9498.9995")


async def test_state_transitions_recorded(tmp_path):
    """验收:订单状态流转正确(状态机推进 + history 落盘可查)。"""
    broker = make_broker(tmp_path, fill_ratio="0.4")
    await broker.submit_order(buy_request(), client_order_id="ai-h1")
    rec = state_json(tmp_path)["orders"]["ai-h1"]
    assert rec["history"] == ["NEW", "PARTIALLY_FILLED"]

    await broker.complete_fill("ai-h1")
    rec = state_json(tmp_path)["orders"]["ai-h1"]
    assert rec["history"] == ["NEW", "PARTIALLY_FILLED", "FILLED"]


async def test_duplicate_submit_is_idempotent(tmp_path):
    """验收:重复提交不会重复记账(同一 client_order_id 幂等)。"""
    broker = make_broker(tmp_path)
    first = await broker.submit_order(buy_request(), client_order_id="ai-d1")
    second = await broker.submit_order(buy_request(), client_order_id="ai-d1")
    assert second == first  # 完全相同的结果
    assert len(broker.trades()) == 1
    account = await broker.get_account()
    assert account.available_usdt == D("9498.9995")  # 只扣一次
    assert len(state_json(tmp_path)["orders"]) == 1


async def test_insufficient_balance_rejected(tmp_path):
    broker = make_broker(
        tmp_path, initial_balances={"USDT": "100"}, price_provider=lambda s: REF
    )
    result = await broker.submit_order(buy_request(), client_order_id="ai-e1")
    assert result.status == OrderStatus.REJECTED
    assert result.error and "余额不足" in result.error
    assert result.exchange_order_id is None  # 从未到达"交易所"
    assert broker.trades() == []


async def test_sell_without_position_rejected(tmp_path):
    broker = make_broker(tmp_path)
    result = await broker.submit_order(sell_request(), client_order_id="ai-e2")
    assert result.status == OrderStatus.REJECTED
    assert result.error and "持仓不足" in result.error


async def test_no_price_rejected_not_guessed(tmp_path):
    """无参考价(无 provider 且无 price_hint)→ 拒绝,不猜测价格。"""
    broker = make_broker(tmp_path, price_provider=None)
    result = await broker.submit_order(buy_request(), client_order_id="ai-e3")
    assert result.status == OrderStatus.REJECTED
    assert result.error and "参考价格" in result.error


async def test_price_hint_fallback(tmp_path):
    broker = make_broker(tmp_path, price_provider=None)
    req = OrderRequest(
        decision_id="cycle-hint", symbol=SYMBOL, side="BUY",
        amount_usdt=D("500.5"), price_hint=REF,
    )
    result = await broker.submit_order(req, client_order_id="ai-hint")
    assert result.status == OrderStatus.FILLED
    assert result.avg_price == D("50050")


async def test_dust_amount_rejected(tmp_path):
    """金额折算数量为 0(精度之下)→ 拒绝,不产生 0 数量订单。"""
    broker = make_broker(tmp_path)
    req = OrderRequest(
        decision_id="cycle-dust", symbol=SYMBOL, side="BUY",
        amount_usdt=D("0.000001"),
    )
    result = await broker.submit_order(req, client_order_id="ai-dust")
    assert result.status == OrderStatus.REJECTED
    assert result.error and "数量为 0" in result.error


async def test_cancel_partial_keeps_filled_part(tmp_path):
    broker = make_broker(tmp_path, fill_ratio="0.4")
    await broker.submit_order(buy_request(), client_order_id="ai-cxl")
    canceled = await broker.cancel_order("ai-cxl")
    assert canceled.status == OrderStatus.CANCELED
    assert canceled.filled_qty == D("0.004")  # 已成交部分保留
    account = await broker.get_account()
    assert account.open_orders_locked_usdt == D("0")  # 锁定释放
    with pytest.raises(InvalidTransitionError):
        await broker.cancel_order("ai-cxl")  # 终态不可再撤销


async def test_complete_fill_rejects_non_partial(tmp_path):
    broker = make_broker(tmp_path)  # fill_ratio=1:全额成交
    await broker.submit_order(buy_request(), client_order_id="ai-f1")
    with pytest.raises(BrokerError):
        await broker.complete_fill("ai-f1")


async def test_persistence_survives_restart(tmp_path):
    """订单与成交持久化:重启(新实例)后订单、余额、成交都还在。"""
    broker1 = make_broker(tmp_path)
    await broker1.submit_order(buy_request(), client_order_id="ai-r1")

    broker2 = make_broker(tmp_path)  # 同一状态文件,模拟重启
    result = await broker2.get_order("ai-r1")
    assert result is not None and result.status == OrderStatus.FILLED
    account = await broker2.get_account()
    assert account.available_usdt == D("9498.9995")
    assert len(broker2.trades("ai-r1")) == 1
    # 重启后同一 ID 再提交依然幂等(不会重复记账)
    again = await broker2.submit_order(buy_request(), client_order_id="ai-r1")
    assert again.filled_qty == D("0.01")
    assert len(broker2.trades()) == 1


async def test_corrupt_state_refuses_to_trade(tmp_path):
    """状态文件损坏 → 构造即抛错,拒绝交易(无法保证幂等就不下单)。"""
    path = tmp_path / "paper.json"
    path.write_text("{{{{not json", encoding="utf-8")
    with pytest.raises(PaperBrokerStateError):
        make_broker(tmp_path)


async def test_slippage_direction(tmp_path):
    """滑点方向:BUY 成交价高于参考,SELL 低于参考。"""
    broker = make_broker(tmp_path, slippage_rate="0.01")
    buy = await broker.submit_order(
        buy_request(amount="505"), client_order_id="ai-sl1"
    )
    assert buy.avg_price == D("50500")  # 50000 × 1.01
    sell = await broker.submit_order(
        sell_request(amount="495"), client_order_id="ai-sl2"
    )
    assert sell.avg_price == D("49500")  # 50000 × 0.99
