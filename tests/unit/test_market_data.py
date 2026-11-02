"""阶段 2:行情数据模型测试。"""

import pytest

from data.market_data import Candle, OHLCVSeries, timeframe_to_ms


def test_timeframe_to_ms():
    assert timeframe_to_ms("15m") == 900_000
    assert timeframe_to_ms("1h") == 3_600_000
    assert timeframe_to_ms("4h") == 14_400_000
    assert timeframe_to_ms("1d") == 86_400_000
    with pytest.raises(ValueError):
        timeframe_to_ms("7m")


def _candle(**kw) -> Candle:
    base = dict(
        symbol="BTC/USDT",
        timeframe="1h",
        open_time=1_700_000_000_000,
        close_time=1_700_003_599_999,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
    )
    base.update(kw)
    return Candle(**base)


def test_candle_ok():
    c = _candle()
    assert c.close == 100.5


def test_candle_rejects_nan():
    with pytest.raises(ValueError):
        _candle(close=float("nan"))


def test_candle_rejects_inf():
    with pytest.raises(ValueError):
        _candle(volume=float("inf"))


def test_candle_rejects_unknown_tf():
    with pytest.raises(ValueError):
        _candle(timeframe="7m")


def test_series_helpers():
    s = OHLCVSeries(
        symbol="BTC/USDT",
        timeframe="1h",
        candles=(_candle(), _candle(open_time=1_700_003_600_000, close_time=1_700_007_199_999)),
    )
    assert len(s) == 2
    assert s.closes == [100.5, 100.5]
    assert s.last is not None and s.last.open_time == 1_700_003_600_000
