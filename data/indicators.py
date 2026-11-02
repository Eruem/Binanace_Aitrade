"""技术指标计算(阶段 2,指导书 7.3)。

纯 Python 实现,不依赖模型计算;LLM 只负责解释结果。
所有函数返回与输入等长的列表,数据不足的位置为 None。

指标:MA20/MA60(即通用 SMA)、RSI14(Wilder)、MACD(12/26/9)、
ATR14(Wilder)、Bollinger(20, 2σ)、成交量均值与变化率。

输入含 NaN/Infinity 时抛 ValueError(校验层应已拦截,这里是第二道防线)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .market_data import OHLCVSeries


def _ensure_finite(values: Sequence[float], name: str) -> None:
    for i, v in enumerate(values):
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"{name}[{i}] 含 NaN/Infinity,拒绝计算指标")


def sma(values: Sequence[float], period: int) -> list[float | None]:
    """简单移动平均。"""
    if period < 1:
        raise ValueError("period 必须 >= 1")
    _ensure_finite(values, "values")
    n = len(values)
    out: list[float | None] = [None] * n
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """指数移动平均,初值用前 period 个的 SMA(标准做法)。"""
    if period < 1:
        raise ValueError("period 必须 >= 1")
    _ensure_finite(values, "values")
    n = len(values)
    out: list[float | None] = [None] * n
    if n < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    """RSI,Wilder 平滑。首个值出现在第 period+1 根。"""
    if period < 1:
        raise ValueError("period 必须 >= 1")
    _ensure_finite(values, "values")
    n = len(values)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, n):
        diff = values[i] - values[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    return out


def macd(
    values: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """MACD:(DIF 线, DEA 线, 柱)。DIF=EMA_fast-EMA_slow;DEA=DIF 的 EMA。"""
    if not (1 <= fast < slow):
        raise ValueError("需要 1 <= fast < slow")
    _ensure_finite(values, "values")
    ef = ema(values, fast)
    es = ema(values, slow)
    dif: list[float | None] = [
        (a - b) if a is not None and b is not None else None
        for a, b in zip(ef, es)
    ]
    # 对 DIF 的非 None 段求 signal EMA,再对齐回原长度
    dea: list[float | None] = [None] * len(values)
    valid_idx = [i for i, v in enumerate(dif) if v is not None]
    if valid_idx:
        dif_valid = [dif[i] for i in valid_idx]  # type: ignore[misc]
        sig = ema(dif_valid, signal)
        for i, s in zip(valid_idx, sig):
            dea[i] = s
    hist: list[float | None] = [
        (d - s) if d is not None and s is not None else None
        for d, s in zip(dif, dea)
    ]
    return dif, dea, hist


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[float | None]:
    """ATR,Wilder 平滑。首个值出现在第 period 根(无前收盘时用 high-low)。"""
    if period < 1:
        raise ValueError("period 必须 >= 1")
    n = len(highs)
    if not (len(lows) == len(closes) == n):
        raise ValueError("highs/lows/closes 长度必须一致")
    _ensure_finite(highs, "highs")
    _ensure_finite(lows, "lows")
    _ensure_finite(closes, "closes")
    out: list[float | None] = [None] * n
    if n == 0:
        return out
    trs: list[float] = []
    for i in range(n):
        if i == 0:
            tr = highs[i] - lows[i]
        else:
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        trs.append(tr)
    if n < period:
        return out
    prev = sum(trs[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def bollinger(
    values: Sequence[float], period: int = 20, mult: float = 2.0
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """布林带:(中轨, 上轨, 下轨)。标准差用总体口径(交易软件默认)。"""
    if period < 1:
        raise ValueError("period 必须 >= 1")
    _ensure_finite(values, "values")
    n = len(values)
    mid: list[float | None] = [None] * n
    up: list[float | None] = [None] * n
    lo: list[float | None] = [None] * n
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / period
        sd = math.sqrt(var)
        mid[i] = m
        up[i] = m + mult * sd
        lo[i] = m - mult * sd
    return mid, up, lo


def volume_roc(volumes: Sequence[float], period: int = 1) -> list[float | None]:
    """成交量变化率:(vol-prev)/prev;分母为 0 时输出 None。"""
    if period < 1:
        raise ValueError("period 必须 >= 1")
    _ensure_finite(volumes, "volumes")
    out: list[float | None] = [None] * len(volumes)
    for i in range(period, len(volumes)):
        prev = volumes[i - period]
        out[i] = None if prev == 0 else (volumes[i] - prev) / prev
    return out


@dataclass(frozen=True)
class IndicatorSnapshot:
    """给 Agent 用的"最新一期指标值"汇总。数据不足的项为 None。"""

    symbol: str
    timeframe: str
    last_close: float
    ma20: float | None
    ma60: float | None
    rsi14: float | None
    macd_dif: float | None
    macd_dea: float | None
    macd_hist: float | None
    atr14: float | None
    bb_mid: float | None
    bb_upper: float | None
    bb_lower: float | None
    volume_ma20: float | None
    volume_roc1: float | None


def compute_snapshot(series: OHLCVSeries) -> IndicatorSnapshot:
    """计算一个序列的最新指标快照。"""
    closes = series.closes
    if not closes:
        raise ValueError("空序列无法计算指标")
    highs = [c.high for c in series.candles]
    lows = [c.low for c in series.candles]
    volumes = series.volumes

    def last_of(xs: list[float | None]) -> float | None:
        return xs[-1] if xs else None

    dif, dea, hist = macd(closes)
    mid, up, lo = bollinger(closes, 20, 2.0)
    return IndicatorSnapshot(
        symbol=series.symbol,
        timeframe=series.timeframe,
        last_close=closes[-1],
        ma20=last_of(sma(closes, 20)),
        ma60=last_of(sma(closes, 60)),
        rsi14=last_of(rsi(closes, 14)),
        macd_dif=last_of(dif),
        macd_dea=last_of(dea),
        macd_hist=last_of(hist),
        atr14=last_of(atr(highs, lows, closes, 14)),
        bb_mid=last_of(mid),
        bb_upper=last_of(up),
        bb_lower=last_of(lo),
        volume_ma20=last_of(sma(volumes, 20)),
        volume_roc1=last_of(volume_roc(volumes, 1)),
    )
