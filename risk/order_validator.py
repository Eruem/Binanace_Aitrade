"""订单验证(阶段 4,指导书 8.1/8.2)。

纯函数式校验,返回违规列表(空列表 = 通过):
- 交易对白名单;
- 方向只允许 BUY / SELL(现货);
- 金额为正且不低于最小名义金额;
- 可用余额(含手续费/滑点占用)足够;
- 总敞口不超限;
- SELL 必须有对应持仓(现货不允许裸卖)。

OrderRequest 是风控与执行层之间的最小订单意图;
执行层(阶段 5/6)不得绕过本验证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class OrderRequest:
    """订单意图(指导书 6.2 OrderRequest 的最小实现)。"""

    decision_id: str
    symbol: str
    side: str  # BUY | SELL
    amount_usdt: Decimal
    price_hint: Decimal | None = None  # 估算用;真实价格由执行层决定


@dataclass(frozen=True)
class AccountView:
    """校验所需账户视图(与 engine.AccountSnapshot 解耦,便于单测)。"""

    equity_usdt: Decimal
    available_usdt: Decimal
    open_exposure_usdt: Decimal = Decimal("0")
    open_orders_locked_usdt: Decimal = Decimal("0")
    held_positions: tuple[tuple[str, Decimal], ...] = field(default_factory=tuple)
    # held_positions: ((symbol, 市值_USDT), ...)

    def position_value(self, symbol: str) -> Decimal:
        for sym, value in self.held_positions:
            if sym == symbol:
                return value
        return Decimal("0")


def validate_order(
    order: OrderRequest,
    account: AccountView,
    *,
    allowed_symbols: tuple[str, ...],
    min_order_usdt: Decimal,
    max_total_exposure_pct: float,
    fee_rate: float,
    slippage_rate: float,
) -> list[str]:
    """返回违规描述列表;空 = 通过。"""
    violations: list[str] = []

    if order.symbol not in allowed_symbols:
        violations.append(f"交易对 {order.symbol} 不在白名单 {allowed_symbols}")
    if order.side not in ("BUY", "SELL"):
        violations.append(f"方向 {order.side!r} 非法(现货仅 BUY/SELL)")
    if order.amount_usdt <= 0:
        violations.append("订单金额必须为正")
    elif order.amount_usdt < min_order_usdt:
        violations.append(
            f"订单金额 {order.amount_usdt} 低于最小名义金额 {min_order_usdt}"
        )

    if order.side == "BUY":
        cost = order.amount_usdt * (
            Decimal("1") + Decimal(str(fee_rate)) + Decimal(str(slippage_rate))
        )
        spendable = account.available_usdt - account.open_orders_locked_usdt
        if cost > spendable:
            violations.append(
                f"余额不足:需要 {cost:.4f}(含费/滑点),可用 {spendable:.4f}"
            )
        exposure_limit = account.equity_usdt * Decimal(str(max_total_exposure_pct))
        headroom = (
            exposure_limit
            - account.open_exposure_usdt
            - account.open_orders_locked_usdt
        )
        if order.amount_usdt > headroom:
            violations.append(
                f"敞口超限:本次 {order.amount_usdt},余量 {headroom:.4f}"
            )
    elif order.side == "SELL":
        held = account.position_value(order.symbol)
        if held <= 0:
            violations.append(f"无持仓可卖: {order.symbol}")
        elif order.amount_usdt > held:
            violations.append(
                f"卖出超过持仓:本次 {order.amount_usdt},持仓市值 {held:.4f}"
            )

    return violations
