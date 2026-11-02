"""阶段 2:指标计算测试(手算基准值)。"""

import math

import pytest

from data.indicators import (
    atr,
    bollinger,
    compute_snapshot,
    ema,
    macd,
    rsi,
    sma,
    volume_roc,
)
from data.market_data import Candle, OHLCVSeries

START = 1_700_000_000_000
H = 3_600_000


def test_sma_basic():
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_sma_constant():
    assert sma([7.0] * 4, 2) == [None, 7.0, 7.0, 7.0]


def test_ema_constant_is_constant():
    out = ema([5.0] * 6, 3)
    assert out[0] is None and out[1] is None
    assert all(v == 5.0 for v in out[2:])


def test_ema_warmup_uses_sma():
    # 前 3 个的 SMA = (1+2+3)/3 = 2
    out = ema([1.0, 2.0, 3.0, 4.0], 3)
    assert out[2] == pytest.approx(2.0)
    # k = 2/4 = 0.5;next = 4*0.5 + 2*0.5 = 3
    assert out[3] == pytest.approx(3.0)


def test_rsi_all_gains_is_100():
    out = rsi([float(i) for i in range(1, 11)], 3)
    assert out[3] == 100.0
    assert out[-1] == 100.0


def test_rsi_hand_computed():
    # values: 10,11,10,11,10,11 period=2
    # 窗口(第1,2个差值): +1, -1 -> avg_gain=0.5 avg_loss=0.5 -> RSI=50
    # i=3: diff +1 -> avg_gain=0.75 avg_loss=0.25 -> RS=3 -> RSI=75
    # i=4: diff -1 -> avg_gain=0.375 avg_loss=0.625 -> RS=0.6 -> RSI=37.5
    # i=5: diff +1 -> avg_gain=0.6875 avg_loss=0.3125 -> RS=2.2 -> RSI=68.75
    out = rsi([10.0, 11.0, 10.0, 11.0, 10.0, 11.0], 2)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(50.0)
    assert out[3] == pytest.approx(75.0)
    assert out[4] == pytest.approx(37.5)
    assert out[5] == pytest.approx(68.75)


def test_macd_constant_series_zero():
    dif, dea, hist = macd([100.0] * 60)
    assert dif[-1] == pytest.approx(0.0, abs=1e-9)
    assert dea[-1] == pytest.approx(0.0, abs=1e-9)
    assert hist[-1] == pytest.approx(0.0, abs=1e-9)


def test_macd_warmup_none():
    dif, dea, hist = macd([1.0, 2.0, 3.0])
    assert all(v is None for v in dif)
    assert all(v is None for v in dea)


def test_atr_constant_range():
    # 每根 high-low=2,无跳空 -> TR=2 -> ATR=2
    n = 20
    highs = [101.0 + i for i in range(n)]
    lows = [99.0 + i for i in range(n)]
    closes = [100.0 + i for i in range(n)]
    out = atr(highs, lows, closes, 14)
    assert out[13] == pytest.approx(2.0)
    assert out[-1] == pytest.approx(2.0)


def test_atr_length_mismatch():
    with pytest.raises(ValueError):
        atr([1.0, 2.0], [1.0], [1.0, 2.0], 2)


def test_bollinger_mid_equals_sma_and_symmetric():
    vals = [float(100 + (i % 7)) for i in range(30)]
    mid, up, lo = bollinger(vals, 20, 2.0)
    ma = sma(vals, 20)
    assert mid[-1] == pytest.approx(ma[-1])
    assert up[-1] - mid[-1] == pytest.approx(mid[-1] - lo[-1])


def test_bollinger_constant_zero_width():
    mid, up, lo = bollinger([5.0] * 25, 20)
    assert up[-1] == mid[-1] == lo[-1] == 5.0


def test_volume_roc():
    assert volume_roc([10.0, 12.0, 9.0], 1) == [None, pytest.approx(0.2), pytest.approx(-0.25)]


def test_volume_roc_zero_denominator():
    assert volume_roc([0.0, 5.0], 1) == [None, None]


def test_non_finite_rejected():
    with pytest.raises(ValueError):
        sma([1.0, float("nan"), 3.0], 2)
    with pytest.raises(ValueError):
        rsi([1.0, float("inf")], 2)


def _series(n: int) -> OHLCVSeries:
    candles = []
    for i in range(n):
        o = START + i * H
        px = 100.0 + i * 0.3 + (i % 5) * 0.2
        candles.append(
            Candle(
                symbol="BTC/USDT",
                timeframe="1h",
                open_time=o,
                close_time=o + H - 1,
                open=px,
                high=px + 1.2,
                low=px - 0.8,
                close=px + 0.4,
                volume=5.0 + (i % 4),
                source="test",
                fetched_at=0,
            )
        )
    return OHLCVSeries(
        symbol="BTC/USDT", timeframe="1h", candles=tuple(candles), source="test"
    )


def test_compute_snapshot_consistency():
    s = _series(80)
    snap = compute_snapshot(s)
    assert snap.ma20 == pytest.approx(sma(s.closes, 20)[-1])
    assert snap.rsi14 == pytest.approx(rsi(s.closes, 14)[-1])
    assert snap.volume_ma20 == pytest.approx(sma(s.volumes, 20)[-1])
    assert snap.last_close == s.closes[-1]
    assert snap.atr14 is not None and snap.atr14 > 0


def test_compute_snapshot_insufficient_data_still_returns_nones():
    s = _series(10)  # 不足 14/20 根
    snap = compute_snapshot(s)
    assert snap.rsi14 is None
    assert snap.ma60 is None
    assert snap.ma20 is None
