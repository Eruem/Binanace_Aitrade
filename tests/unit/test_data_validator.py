"""阶段 2:数据质量校验测试(指导书 14.5/14.6 相关)。"""

from __future__ import annotations

from data.data_validator import validate_alignment, validate_series
from data.market_data import Candle, OHLCVSeries

START = 1_700_000_000_000  # 对齐整点
H = 3_600_000
M15 = 900_000


def make_candles(
    n: int,
    tf_ms: int,
    *,
    start: int = START,
    timeframe: str = "1h",
    base: float = 100.0,
) -> list[Candle]:
    out = []
    for i in range(n):
        o = start + i * tf_ms
        px = base + i * 0.5
        out.append(
            Candle(
                symbol="BTC/USDT",
                timeframe=timeframe,
                open_time=o,
                close_time=o + tf_ms - 1,
                open=px,
                high=px + 1,
                low=px - 1,
                close=px + 0.25,
                volume=10.0 + i,
                source="test",
                fetched_at=0,
            )
        )
    return out


def make_series(n: int = 24, tf_ms: int = H, timeframe: str = "1h", **kw) -> OHLCVSeries:
    candles = make_candles(n, tf_ms, timeframe=timeframe, **kw)
    return OHLCVSeries(
        symbol="BTC/USDT",
        timeframe=timeframe,
        candles=tuple(candles),
        source="test",
        fetched_at=0,
    )


def now_after(series: OHLCVSeries) -> int:
    return series.candles[-1].close_time + 1


class TestOk:
    def test_clean_series_passes(self):
        s = make_series()
        r = validate_series(s, now_ms=now_after(s))
        assert r.ok, r.errors

    def test_empty_fails(self):
        s = OHLCVSeries(symbol="BTC/USDT", timeframe="1h", candles=())
        r = validate_series(s, now_ms=START)
        assert not r.ok


class TestOrdering:
    def test_duplicate_timestamp(self):
        candles = make_candles(5, H)
        candles[3] = candles[3].model_copy(update={"open_time": candles[2].open_time})
        s = OHLCVSeries(symbol="BTC/USDT", timeframe="1h", candles=tuple(candles))
        r = validate_series(s, now_ms=now_after(s))
        assert not r.ok
        assert any("重复" in e for e in r.errors)

    def test_unordered(self):
        candles = make_candles(5, H)
        candles[2], candles[3] = candles[3], candles[2]
        s = OHLCVSeries(symbol="BTC/USDT", timeframe="1h", candles=tuple(candles))
        r = validate_series(s, now_ms=now_after(s))
        assert not r.ok
        assert any("倒序" in e for e in r.errors)

    def test_gap(self):
        candles = make_candles(6, H)
        del candles[3]  # 制造一个 1 根的缺口
        s = OHLCVSeries(symbol="BTC/USDT", timeframe="1h", candles=tuple(candles))
        r = validate_series(s, now_ms=now_after(s))
        assert not r.ok
        assert any("缺失" in e for e in r.errors)


class TestOhlcLogic:
    def test_high_below_low(self):
        candles = make_candles(3, H)
        candles[1] = candles[1].model_copy(update={"high": 50.0, "low": 60.0})
        s = OHLCVSeries(symbol="BTC/USDT", timeframe="1h", candles=tuple(candles))
        r = validate_series(s, now_ms=now_after(s))
        assert not r.ok
        assert any("high < low" in e for e in r.errors)

    def test_high_below_close(self):
        candles = make_candles(3, H)
        candles[1] = candles[1].model_copy(update={"high": 1.0})
        s = OHLCVSeries(symbol="BTC/USDT", timeframe="1h", candles=tuple(candles))
        r = validate_series(s, now_ms=now_after(s))
        assert not r.ok

    def test_negative_volume(self):
        candles = make_candles(3, H)
        candles[0] = candles[0].model_copy(update={"volume": -1.0})
        s = OHLCVSeries(symbol="BTC/USDT", timeframe="1h", candles=tuple(candles))
        r = validate_series(s, now_ms=now_after(s))
        assert not r.ok

    def test_nan_injected_late(self):
        # Candle 构造会拒 NaN;这里模拟序列中途被污染的场景
        candles = make_candles(3, H)
        candles[2] = candles[2].model_copy(update={"close": float("nan")})
        s = OHLCVSeries(symbol="BTC/USDT", timeframe="1h", candles=tuple(candles))
        r = validate_series(s, now_ms=now_after(s))
        assert not r.ok
        assert any("NaN" in e for e in r.errors)


class TestClosedAndStale:
    def test_unclosed_last_candle(self):
        s = make_series()
        now = s.candles[-1].close_time - 1000  # 还没收盘
        r = validate_series(s, now_ms=now)
        assert not r.ok
        assert any("尚未收盘" in e for e in r.errors)

    def test_closed_last_allowed_when_relaxed(self):
        s = make_series()
        now = s.candles[-1].close_time - 1000
        r = validate_series(s, now_ms=now, require_closed_last=False)
        assert r.ok, r.errors

    def test_stale(self):
        s = make_series(n=5)
        now = s.candles[-1].close_time + 10 * H  # 过期 10 小时
        r = validate_series(s, now_ms=now, max_staleness_ms=2 * H)
        assert not r.ok
        assert any("过期" in e for e in r.errors)

    def test_fresh_ok(self):
        s = make_series(n=5)
        now = s.candles[-1].close_time + 30 * 60 * 1000
        r = validate_series(s, now_ms=now, max_staleness_ms=2 * H)
        assert r.ok, r.errors


class TestAlignment:
    def test_aligned_timeframes(self):
        h1 = make_series(n=10, tf_ms=H, timeframe="1h")
        m15 = make_series(n=40, tf_ms=M15, timeframe="15m")
        r = validate_alignment([m15, h1], now_ms=now_after(h1))
        assert r.ok, r.errors

    def test_15m_ahead_of_1h_fails(self):
        h1 = make_series(n=10, tf_ms=H, timeframe="1h")
        m15 = make_series(n=41, tf_ms=M15, timeframe="15m")  # 多出一根
        r = validate_alignment([m15, h1], now_ms=now_after(m15))
        assert not r.ok
        assert any("不齐" in e or "不一致" in e for e in r.errors)

    def test_single_series_trivially_ok(self):
        h1 = make_series(n=10, tf_ms=H, timeframe="1h")
        r = validate_alignment([h1], now_ms=now_after(h1))
        assert r.ok
