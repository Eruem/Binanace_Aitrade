"""阶段 4:Risk Engine 测试(指导书 13 节阶段 4 验收 + 14 节安全项)。"""

from __future__ import annotations

from decimal import Decimal

from agents.schemas import Action, StrategyName, TradingDecision
from config.loader import RiskParams
from risk.daily_loss import DailyLossTracker
from risk.engine import AccountSnapshot, Position, RiskEngine
from risk.kill_switch import KillSwitch

D = Decimal
NOW = 1_793_145_600_000
SYMBOL = "BTC/USDT"


def make_decision(**over) -> TradingDecision:
    base = dict(
        decision_id="cycle-test-0001",
        symbol=SYMBOL,
        action=Action.BUY,
        strategy=StrategyName.DCA,
        amount_usdt=D("50"),
        confidence=0.5,
        reason="test",
        created_at=NOW - 1000,
        expires_at=NOW + 3_600_000,
    )
    base.update(over)
    return TradingDecision(**base)


def make_account(**over) -> AccountSnapshot:
    base = dict(
        equity_usdt=D("1000"),
        available_usdt=D("1000"),
        open_exposure_usdt=D("0"),
        open_orders_locked_usdt=D("0"),
        positions=(),
        daily_realized_pnl_usdt=D("0"),
        consecutive_losses=0,
        fetched_at=NOW,
    )
    base.update(over)
    return AccountSnapshot(**base)


def make_engine(**over) -> RiskEngine:
    params = over.pop("params", None) or RiskParams()
    return RiskEngine(
        params=params,
        allowed_symbols=(SYMBOL,),
        kill_switch=over.pop("kill_switch", None),
        daily_loss=over.pop("daily_loss", None),
        audit_path=over.pop("audit_path", None),
    )


class TestApprovePath:
    def test_clean_buy_approved(self):
        engine = make_engine()
        r = engine.evaluate(make_decision(), make_account(), now_ms=NOW)
        assert r.verdict == "APPROVED"
        assert r.sized_amount_usdt == D("50.00")
        assert r.order is not None and r.order.side == "BUY"
        # 审计:每条规则都有记录
        rules = {c.rule for c in r.checks}
        assert {
            "risk_enabled", "kill_switch", "expired_signal", "symbol_whitelist",
            "data_error", "duplicate_order", "daily_loss", "consecutive_losses",
            "single_trade_risk", "order_validator",
        } <= rules

    def test_sized_down_by_risk_cap(self):
        engine = make_engine(params=RiskParams(max_single_order_equity_pct=0.9))
        r = engine.evaluate(
            make_decision(amount_usdt=D("500")), make_account(), now_ms=NOW
        )
        # 风险帽: 1000×0.005/0.02 = 250
        assert r.sized_amount_usdt == D("250.00")
        assert r.verdict == "APPROVED"

    def test_hold_is_no_order(self):
        engine = make_engine()
        r = engine.evaluate(
            make_decision(action=Action.HOLD, strategy=StrategyName.NO_TRADE,
                          amount_usdt=None),
            make_account(),
            now_ms=NOW,
        )
        assert r.verdict == "NO_ORDER"
        assert not r.approved and not r.reasons.count("x")


class TestRejections:
    def test_expired_signal_rejected(self):
        engine = make_engine()
        r = engine.evaluate(
            make_decision(expires_at=NOW - 1), make_account(), now_ms=NOW
        )
        assert r.verdict == "REJECTED"
        assert any("过期" in reason for reason in r.reasons)

    def test_symbol_not_whitelisted(self):
        engine = make_engine()
        r = engine.evaluate(
            make_decision(symbol="DOGE/USDT"), make_account(), now_ms=NOW
        )
        assert r.verdict == "REJECTED"

    def test_data_errors_reject(self):
        engine = make_engine()
        r = engine.evaluate(
            make_decision(), make_account(), now_ms=NOW,
            data_errors=["[1h] 缺口"],
        )
        assert r.verdict == "REJECTED"
        assert any("数据错误" in reason for reason in r.reasons)

    def test_duplicate_order_rejected(self):
        engine = make_engine()
        engine.register_executed("cycle-test-0001")
        r = engine.evaluate(make_decision(), make_account(), now_ms=NOW)
        assert r.verdict == "REJECTED"
        assert any("重复" in reason for reason in r.reasons)

    def test_insufficient_balance_rejected(self):
        # 可用余额为 0 -> 仓位计算为 0,直接否决(余额不足的引擎路径)
        engine = make_engine()
        r = engine.evaluate(
            make_decision(amount_usdt=D("90")),
            make_account(available_usdt=D("0")),
            now_ms=NOW,
        )
        assert r.verdict == "REJECTED"
        assert any("不足" in reason for reason in r.reasons)

    def test_too_small_balance_rejected_below_min_notional(self):
        # 余额仅够 0.99 -> 低于最小名义金额 5 -> 否决
        engine = make_engine()
        r = engine.evaluate(
            make_decision(amount_usdt=D("90")),
            make_account(available_usdt=D("1")),
            now_ms=NOW,
        )
        assert r.verdict == "REJECTED"
        assert any("最小名义金额" in reason for reason in r.reasons)

    def test_below_min_notional_rejected(self):
        engine = make_engine()
        r = engine.evaluate(
            make_decision(amount_usdt=D("2")),
            make_account(),
            now_ms=NOW,
        )
        assert r.verdict == "REJECTED"
        assert any("最小名义金额" in reason for reason in r.reasons)

    def test_exposure_cap_rejected_when_sizing_zero(self):
        engine = make_engine()
        r = engine.evaluate(
            make_decision(),
            make_account(open_exposure_usdt=D("300")),  # 敞口已满
            now_ms=NOW,
        )
        assert r.verdict == "REJECTED"

    def test_daily_loss_blocks_buy(self):
        tracker = DailyLossTracker()
        tracker.record(D("-25"))
        engine = make_engine(daily_loss=tracker)
        r = engine.evaluate(make_decision(), make_account(), now_ms=NOW)
        assert r.verdict == "REJECTED"
        assert any("当日" in reason for reason in r.reasons)

    def test_daily_loss_does_not_block_sell(self):
        tracker = DailyLossTracker()
        tracker.record(D("-25"))
        engine = make_engine(daily_loss=tracker)
        account = make_account(
            positions=(Position(SYMBOL, D("0.001"), D("50000")),),
        )
        r = engine.evaluate(
            make_decision(action=Action.SELL, amount_usdt=D("20")),
            account,
            now_ms=NOW,
        )
        assert r.verdict == "APPROVED"

    def test_consecutive_losses_block_buy(self):
        engine = make_engine()
        r = engine.evaluate(
            make_decision(),
            make_account(consecutive_losses=3),
            now_ms=NOW,
        )
        assert r.verdict == "REJECTED"
        assert any("连续亏损" in reason for reason in r.reasons)

    def test_kill_switch_blocks_buy(self):
        ks = KillSwitch()
        ks.engage("测试", source="manual")
        engine = make_engine(kill_switch=ks)
        r = engine.evaluate(make_decision(), make_account(), now_ms=NOW)
        assert r.verdict == "REJECTED"
        assert any("Kill Switch" in reason for reason in r.reasons)

    def test_kill_switch_allows_sell(self):
        ks = KillSwitch()
        ks.auto_pause("异常")
        engine = make_engine(kill_switch=ks)
        account = make_account(
            positions=(Position(SYMBOL, D("0.001"), D("50000")),),
        )
        r = engine.evaluate(
            make_decision(action=Action.SELL, amount_usdt=D("20")),
            account,
            now_ms=NOW,
        )
        assert r.verdict == "APPROVED"

    def test_sell_without_position_rejected(self):
        engine = make_engine()
        r = engine.evaluate(
            make_decision(action=Action.SELL, amount_usdt=D("20")),
            make_account(),  # 无持仓
            now_ms=NOW,
        )
        assert r.verdict == "REJECTED"
        assert any("无持仓" in reason for reason in r.reasons)

    def test_sell_over_position_rejected(self):
        engine = make_engine()
        account = make_account(
            positions=(Position(SYMBOL, D("0.0004"), D("50000")),),  # 市值 20
        )
        r = engine.evaluate(
            make_decision(action=Action.SELL, amount_usdt=D("50")),
            account,
            now_ms=NOW,
        )
        assert r.verdict == "REJECTED"

    def test_disabled_engine_rejects_everything(self):
        engine = make_engine(params=RiskParams(enabled=False))
        r = engine.evaluate(make_decision(), make_account(), now_ms=NOW)
        assert r.verdict == "REJECTED"


class TestAudit:
    def test_audit_jsonl_written(self, tmp_path):
        audit = tmp_path / "risk.jsonl"
        engine = make_engine(audit_path=audit)
        engine.evaluate(make_decision(), make_account(), now_ms=NOW)
        content = audit.read_text(encoding="utf-8")
        assert "cycle-test-0001" in content
        assert "APPROVED" in content

    def test_audit_records_rejection_checks(self, tmp_path):
        audit = tmp_path / "risk.jsonl"
        engine = make_engine(audit_path=audit)
        r = engine.evaluate(
            make_decision(), make_account(available_usdt=D("0")), now_ms=NOW
        )
        assert r.verdict == "REJECTED"
        last = audit.read_text(encoding="utf-8").strip().splitlines()[-1]
        assert "仓位计算结果为 0" in last

    def test_engine_has_no_llm_interface(self):
        # 风控引擎不得暴露任何模型/LLM 相关属性
        engine = make_engine()
        assert not [a for a in dir(engine) if "llm" in a.lower() or "model" in a.lower()]
