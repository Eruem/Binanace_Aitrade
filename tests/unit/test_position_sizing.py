"""阶段 4:仓位计算测试(手算基准)。"""

from decimal import Decimal

from risk.position_sizing import size_buy_amount

D = Decimal


def _size(**over):
    base = dict(
        decision_amount=D("100"),
        equity=D("1000"),
        available=D("1000"),
        open_exposure=D("0"),
        open_orders_locked=D("0"),
        max_single_order_equity_pct=0.10,
        max_total_exposure_pct=0.30,
        max_single_trade_risk_pct=0.005,
        assumed_stop_loss_pct=0.02,
        fee_rate=0.001,
        slippage_rate=0.0005,
    )
    base.update(over)
    return size_buy_amount(**base)


def test_capped_by_decision_amount():
    amount, caps = _size(decision_amount=D("50"))
    assert amount == D("50.00")
    assert any("决策建议金额" in c for c in caps)


def test_capped_by_single_order_pct():
    # 权益 1000 × 10% = 100;决策要 500
    amount, _ = _size(decision_amount=D("500"))
    assert amount == D("100.00")


def test_capped_by_exposure_headroom():
    # 敞口上限 300,已用 250 -> 余量 50
    amount, _ = _size(
        decision_amount=D("500"), open_exposure=D("250")
    )
    assert amount == D("50.00")


def test_capped_by_available_balance_with_costs():
    # 可用 50,成本系数 1.0015 -> 50/1.0015 ≈ 49.92
    amount, _ = _size(decision_amount=D("500"), available=D("50"))
    assert amount == D("49.92")


def test_capped_by_single_trade_risk():
    # 风险上限 = 1000×0.005 / 0.02 = 250;决策要 500 -> 250
    amount, _ = _size(decision_amount=D("500"))
    # 单笔 10% 上限(100)先生效;把单笔上限放大验证风险帽
    amount2, _ = _size(
        decision_amount=D("500"), max_single_order_equity_pct=0.9
    )
    assert amount == D("100.00")
    assert amount2 == D("250.00")


def test_zero_when_exhausted():
    amount, _ = _size(open_exposure=D("300"))  # 敞口已满
    assert amount == D("0.00")


def test_quantized_to_two_decimals():
    amount, _ = _size(
        decision_amount=D("33.333"), max_single_order_equity_pct=0.9
    )
    assert amount == D("33.33")
