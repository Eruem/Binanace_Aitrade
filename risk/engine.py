"""Risk Engine(阶段 4,指导书 8.1)。

普通 Python 确定性代码,拥有最终否决权。LLM 不能:
修改限额、自行审批、覆盖 Kill Switch、在风控失败后要求执行、
把 HOLD 改成 BUY/SELL(输出 Schema 已拦)。

规则顺序(任一 REJECT 即否决;全部记录,便于审计):
1. 风控启用(risk.enabled=false 视为停机,拒绝一切新订单);
2. Kill Switch(禁止新开仓;SELL 平仓不受限);
3. HOLD → NO_ORDER(无订单可审);
4. 信号过期(expires_at);
5. 交易对白名单;
6. 数据错误停机(stop_on_data_error);
7. 重复订单(decision_id 已执行);
8. 日亏损上限(禁止新开仓;SELL 不受限);
9. 连续亏损(禁止新开仓;SELL 不受限);
10. 仓位计算 + 单笔风险上限(position_sizing);
11. 订单验证(order_validator:余额/敞口/最小名义/裸卖)。

风控结果 RiskResult 逐条记录规则、结论与细节,可审计、可落盘。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Literal

from agents.schemas import Action, TradingDecision

from .daily_loss import DailyLossTracker, DailyLossStateError
from .kill_switch import KillSwitch
from .order_validator import AccountView, OrderRequest, validate_order
from .position_sizing import size_buy_amount

RiskVerdict = Literal["APPROVED", "REJECTED", "NO_ORDER"]


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: Decimal
    entry_price: Decimal

    @property
    def value_usdt(self) -> Decimal:
        return self.qty * self.entry_price


@dataclass(frozen=True)
class AccountSnapshot:
    """账户快照(阶段 5/6 由执行层提供真实数据)。"""

    equity_usdt: Decimal
    available_usdt: Decimal
    open_exposure_usdt: Decimal = Decimal("0")
    open_orders_locked_usdt: Decimal = Decimal("0")
    positions: tuple[Position, ...] = ()
    daily_realized_pnl_usdt: Decimal = Decimal("0")
    consecutive_losses: int = 0
    fetched_at: int = 0

    def position_value(self, symbol: str) -> Decimal:
        return sum(
            (p.value_usdt for p in self.positions if p.symbol == symbol),
            Decimal("0"),
        )


@dataclass(frozen=True)
class RuleCheck:
    rule: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class RiskResult:
    verdict: RiskVerdict
    decision_id: str
    symbol: str
    reasons: tuple[str, ...] = ()
    checks: tuple[RuleCheck, ...] = ()
    sized_amount_usdt: Decimal | None = None
    sizing_caps: tuple[str, ...] = ()
    checked_at: int = 0
    order: OrderRequest | None = None

    @property
    def approved(self) -> bool:
        return self.verdict == "APPROVED"

    def to_audit_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "decision_id": self.decision_id,
            "symbol": self.symbol,
            "reasons": list(self.reasons),
            "checks": [
                {"rule": c.rule, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
            "sized_amount_usdt": (
                str(self.sized_amount_usdt) if self.sized_amount_usdt is not None
                else None
            ),
            "sizing_caps": list(self.sizing_caps),
            "checked_at": self.checked_at,
        }


class RiskEngine:
    def __init__(
        self,
        *,
        params: object,  # config.RiskParams
        allowed_symbols: Iterable[str],
        kill_switch: KillSwitch | None = None,
        daily_loss: DailyLossTracker | None = None,
        audit_path: str | Path | None = None,
    ) -> None:
        self.p = params
        self.allowed_symbols = tuple(allowed_symbols)
        self.kill_switch = kill_switch or KillSwitch()
        self.daily_loss = daily_loss or DailyLossTracker()
        self._audit_path = Path(audit_path) if audit_path else None
        # 已执行 decision_id 防重(阶段 5 起由执行层在成交后注册)
        self._executed: set[str] = set()

    # ------------------------------------------------------------------
    def register_executed(self, decision_id: str) -> None:
        """执行层在订单提交成功后注册,防止同 decision 重复下单。"""
        self._executed.add(decision_id)

    def was_executed(self, decision_id: str) -> bool:
        return decision_id in self._executed

    # ------------------------------------------------------------------
    def evaluate(
        self,
        decision: TradingDecision,
        account: AccountSnapshot,
        *,
        now_ms: int | None = None,
        data_errors: tuple[str, ...] | list[str] = (),
    ) -> RiskResult:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        checks: list[RuleCheck] = []
        reasons: list[str] = []

        def reject(rule: str, detail: str) -> RiskResult:
            checks.append(RuleCheck(rule, False, detail))
            reasons.append(detail)
            self._audit(decision, checks, reasons, None, (), now, "REJECTED")
            return RiskResult(
                verdict="REJECTED",
                decision_id=decision.decision_id,
                symbol=decision.symbol,
                reasons=tuple(reasons),
                checks=tuple(checks),
                checked_at=now,
            )

        # 1. 风控启用
        if not self.p.enabled:
            return reject("risk_enabled", "风控被禁用,拒绝一切新订单(安全默认)")
        checks.append(RuleCheck("risk_enabled", True, "风控已启用"))

        # 2. Kill Switch:只禁止新开仓;SELL 平仓不受限
        if self.kill_switch.is_engaged:
            if decision.action == Action.BUY:
                return reject(
                    "kill_switch",
                    f"Kill Switch 已触发({self.kill_switch.state.reason}),禁止新开仓",
                )
            checks.append(
                RuleCheck("kill_switch", True, "已触发,但 SELL 平仓不受限")
            )
        else:
            checks.append(RuleCheck("kill_switch", True, "未触发"))

        # 3. HOLD → 无订单
        if decision.action == Action.HOLD:
            checks.append(RuleCheck("hold", True, "HOLD 不产生订单"))
            result = RiskResult(
                verdict="NO_ORDER",
                decision_id=decision.decision_id,
                symbol=decision.symbol,
                reasons=("HOLD 不产生订单",),
                checks=tuple(checks),
                checked_at=now,
            )
            self._audit(decision, checks, reasons, None, (), now, "NO_ORDER")
            return result

        # 4. 信号过期
        if self.p.reject_expired_signal and decision.expires_at <= now:
            return reject(
                "expired_signal",
                f"信号已过期(expires_at={decision.expires_at} <= now={now})",
            )
        checks.append(RuleCheck("expired_signal", True, "信号在有效期内"))

        # 5. 交易对白名单
        if decision.symbol not in self.allowed_symbols:
            return reject(
                "symbol_whitelist",
                f"交易对 {decision.symbol} 不在白名单 {self.allowed_symbols}",
            )
        checks.append(RuleCheck("symbol_whitelist", True, "在白名单内"))

        # 6. 数据错误停机
        if self.p.stop_on_data_error and data_errors:
            return reject("data_error", f"存在数据错误,停止开仓: {data_errors[:3]}")
        checks.append(RuleCheck("data_error", True, "无数据错误"))

        # 7. 重复订单
        if self.p.reject_duplicate_order and self.was_executed(decision.decision_id):
            return reject(
                "duplicate_order",
                f"decision_id {decision.decision_id} 已执行过,拒绝重复下单",
            )
        checks.append(RuleCheck("duplicate_order", True, "非重复订单"))

        # 8. 日亏损上限(仅禁新开仓)
        if decision.action == Action.BUY:
            try:
                status = self.daily_loss.status(
                    equity_usdt=account.equity_usdt,
                    max_daily_loss_pct=self.p.max_daily_loss_pct,
                )
            except DailyLossStateError as e:
                return reject("daily_loss", f"日亏损状态不可用: {e}")
            if status.breached:
                return reject(
                    "daily_loss",
                    f"当日已实现亏损 {status.realized_pnl_usdt} 达到上限 "
                    f"-{status.limit_usdt},禁止新开仓",
                )
            checks.append(
                RuleCheck(
                    "daily_loss",
                    True,
                    f"当日盈亏 {status.realized_pnl_usdt},未触限(-{status.limit_usdt})",
                )
            )

            # 9. 连续亏损(仅禁新开仓)
            if account.consecutive_losses >= self.p.max_consecutive_losses:
                return reject(
                    "consecutive_losses",
                    f"连续亏损 {account.consecutive_losses} 次"
                    f"(上限 {self.p.max_consecutive_losses}),禁止新开仓",
                )
            checks.append(
                RuleCheck(
                    "consecutive_losses",
                    True,
                    f"连续亏损 {account.consecutive_losses} 次,未触限",
                )
            )

        # 10. 仓位计算 + 单笔风险上限(仅 BUY)
        sized: Decimal | None = None
        caps: tuple[str, ...] = ()
        if decision.action == Action.BUY:
            sized, cap_labels = size_buy_amount(
                decision_amount=decision.amount_usdt,
                equity=account.equity_usdt,
                available=account.available_usdt,
                open_exposure=account.open_exposure_usdt,
                open_orders_locked=account.open_orders_locked_usdt,
                max_single_order_equity_pct=self.p.max_single_order_equity_pct,
                max_total_exposure_pct=self.p.max_total_exposure_pct,
                max_single_trade_risk_pct=self.p.max_single_trade_risk_pct,
                assumed_stop_loss_pct=self.p.assumed_stop_loss_pct,
                fee_rate=self.p.fee_rate,
                slippage_rate=self.p.slippage_rate,
            )
            caps = tuple(cap_labels)
            if sized <= 0:
                return reject(
                    "position_sizing",
                    "仓位计算结果为 0(额度/余额/敞口不足)",
                )
            risk_usdt = sized * Decimal(str(self.p.assumed_stop_loss_pct))
            risk_limit = account.equity_usdt * Decimal(
                str(self.p.max_single_trade_risk_pct)
            )
            if risk_usdt > risk_limit:
                return reject(
                    "single_trade_risk",
                    f"单笔风险 {risk_usdt:.4f} 超过上限 {risk_limit:.4f}",
                )
            checks.append(
                RuleCheck(
                    "single_trade_risk",
                    True,
                    f"单笔风险 {risk_usdt:.4f} <= 上限 {risk_limit:.4f}"
                    "(按 assumed_stop_loss_pct 估算)",
                )
            )
        else:
            sized = decision.amount_usdt
            caps = ("SELL 使用决策金额",)

        # 11. 订单验证
        order = OrderRequest(
            decision_id=decision.decision_id,
            symbol=decision.symbol,
            side=decision.action.value,
            amount_usdt=sized,
        )
        violations = validate_order(
            order,
            AccountView(
                equity_usdt=account.equity_usdt,
                available_usdt=account.available_usdt,
                open_exposure_usdt=account.open_exposure_usdt,
                open_orders_locked_usdt=account.open_orders_locked_usdt,
                held_positions=tuple(
                    (p.symbol, p.value_usdt) for p in account.positions
                ),
            ),
            allowed_symbols=self.allowed_symbols,
            min_order_usdt=Decimal(str(self.p.min_order_usdt)),
            max_total_exposure_pct=self.p.max_total_exposure_pct,
            fee_rate=self.p.fee_rate,
            slippage_rate=self.p.slippage_rate,
        )
        if violations:
            for v in violations:
                checks.append(RuleCheck("order_validator", False, v))
            self._audit(
                decision, checks, list(violations), sized, caps, now, "REJECTED"
            )
            return RiskResult(
                verdict="REJECTED",
                decision_id=decision.decision_id,
                symbol=decision.symbol,
                reasons=tuple(violations),
                checks=tuple(checks),
                sized_amount_usdt=sized,
                sizing_caps=caps,
                checked_at=now,
            )
        checks.append(RuleCheck("order_validator", True, "订单校验通过"))

        result = RiskResult(
            verdict="APPROVED",
            decision_id=decision.decision_id,
            symbol=decision.symbol,
            reasons=("全部规则通过",),
            checks=tuple(checks),
            sized_amount_usdt=sized,
            sizing_caps=caps,
            checked_at=now,
            order=order,
        )
        self._audit(decision, checks, [], sized, caps, now, "APPROVED")
        return result

    # ------------------------------------------------------------------
    def _audit(
        self,
        decision: TradingDecision,
        checks: list[RuleCheck],
        reasons: list[str],
        sized: Decimal | None,
        caps: tuple[str, ...],
        now: int,
        verdict: str,
    ) -> None:
        if not self._audit_path:
            return
        entry = {
            "decision_id": decision.decision_id,
            "symbol": decision.symbol,
            "action": decision.action.value,
            "verdict": verdict,
            "reasons": list(reasons),
            "checks": [
                {"rule": c.rule, "passed": c.passed, "detail": c.detail}
                for c in checks
            ],
            "sized_amount_usdt": str(sized) if sized is not None else None,
            "sizing_caps": list(caps),
            "checked_at": now,
        }
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
