"""阶段 4:日亏损限制测试。"""

from decimal import Decimal

import pytest

from risk.daily_loss import DailyLossStateError, DailyLossTracker, utc_day

D = Decimal


def test_no_loss_not_breached():
    t = DailyLossTracker()
    s = t.status(equity_usdt=D("1000"), max_daily_loss_pct=0.02)
    assert not s.breached
    assert s.limit_usdt == D("20.00")


def test_breached_at_limit():
    t = DailyLossTracker()
    t.record(D("-20"))
    s = t.status(equity_usdt=D("1000"), max_daily_loss_pct=0.02)
    assert s.breached  # 亏损 <= -限额 即触限


def test_below_limit_ok():
    t = DailyLossTracker()
    t.record(D("-19.99"))
    s = t.status(equity_usdt=D("1000"), max_daily_loss_pct=0.02)
    assert not s.breached


def test_profit_offsets_loss():
    t = DailyLossTracker()
    t.record(D("-15"))
    t.record(D("10"))  # 净 -5
    s = t.status(equity_usdt=D("1000"), max_daily_loss_pct=0.02)
    assert not s.breached
    assert t.realized_today() == D("-5")


def test_day_isolation():
    t = DailyLossTracker()
    t.record(D("-50"), day="2026-10-30")
    s = t.status(
        equity_usdt=D("1000"), max_daily_loss_pct=0.02, day="2026-10-31"
    )
    assert not s.breached  # 昨天的亏损不影响今天


def test_persistence(tmp_path):
    path = tmp_path / "dl.json"
    t1 = DailyLossTracker(path)
    t1.record(D("-25"))
    t2 = DailyLossTracker(path)  # 重启
    s = t2.status(equity_usdt=D("1000"), max_daily_loss_pct=0.02)
    assert s.breached
    assert s.realized_pnl_usdt == D("-25")


def test_corrupt_state_refuses_to_trade(tmp_path):
    path = tmp_path / "dl.json"
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(DailyLossStateError):
        DailyLossTracker(path)


def test_utc_day():
    assert utc_day(0) == "1970-01-01"
