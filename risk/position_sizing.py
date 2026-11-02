"""仓位计算(阶段 4,指导书 8.2)。

单笔风险定义(必须与文档一致,不得把订单金额百分比当成最大亏损):
    单笔交易风险 = 止损距离比例 × 订单金额
                ≈ assumed_stop_loss_pct × 订单金额
    上限:max_single_trade_risk_pct × 权益

BUY 金额取以下上限的最小值:
1. 决策建议金额(amount_usdt);
2. max_single_order_equity_pct × 权益;
3. 敞口余量 = max_total_exposure_pct × 权益 −(已有敞口 + 挂单占用);
4. (可用余额 − 挂单占用)/(1 + fee + slippage);
5. 单笔风险上限换算:max_single_trade_risk_pct × 权益 / assumed_stop_loss_pct。

结果按 0.01 USDT 取整(真实交易对精度由阶段 6 交易所规则再约束)。
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

Q2 = Decimal("0.01")

D = Decimal


def _pct(value: Decimal | float | int) -> Decimal:
    return Decimal(str(value))


def size_buy_amount(
    *,
    decision_amount: Decimal | None,
    equity: Decimal,
    available: Decimal,
    open_exposure: Decimal,
    open_orders_locked: Decimal,
    max_single_order_equity_pct: float,
    max_total_exposure_pct: float,
    max_single_trade_risk_pct: float,
    assumed_stop_loss_pct: float,
    fee_rate: float,
    slippage_rate: float,
) -> tuple[Decimal, list[str]]:
    """计算 BUY 目标金额与每个生效上限的说明(审计用)。

    Returns:
        (amount, caps_applied): amount 为 0 表示无可行仓位;
        caps_applied 为生效约束的描述列表。
    """
    caps: list[tuple[Decimal, str]] = []

    if decision_amount is not None and decision_amount > 0:
        caps.append((_pct(decision_amount), "决策建议金额"))

    equity_cap = equity * _pct(max_single_order_equity_pct)
    caps.append((equity_cap, f"单笔上限={max_single_order_equity_pct:.2%}×权益"))

    exposure_limit = equity * _pct(max_total_exposure_pct)
    headroom = exposure_limit - open_exposure - open_orders_locked
    caps.append((headroom, "敞口余量"))

    cost_factor = Decimal("1") + _pct(fee_rate) + _pct(slippage_rate)
    cash_cap = (available - open_orders_locked) / cost_factor
    caps.append((cash_cap, "可用余额(含手续费/滑点占用)"))

    risk_cap = (
        equity * _pct(max_single_trade_risk_pct) / _pct(assumed_stop_loss_pct)
    )
    caps.append((risk_cap, f"单笔风险上限={max_single_trade_risk_pct:.2%}×权益/止损比例"))

    if not caps:
        return Decimal("0.00"), []

    chosen = min(c for c, _ in caps)
    applied = [label for c, label in caps if c == chosen]
    if chosen <= 0:
        return Decimal("0.00"), applied or ["无可用额度"]

    amount = chosen.quantize(Q2, rounding=ROUND_DOWN)
    if amount <= 0:
        return Decimal("0.00"), applied
    return amount, applied
