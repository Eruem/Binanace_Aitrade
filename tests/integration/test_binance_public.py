"""阶段 2 集成测试:真实读取 Binance 公开行情(无需 API Key)。

网络不可用时自动跳过,不影响其余测试。
"""

from __future__ import annotations

import time

import pytest

from data.binance_data import BinancePublicData, PublicDataError
from data.data_validator import validate_series
from data.indicators import compute_snapshot


async def test_public_btc_ohlcv_end_to_end():
    client = BinancePublicData()
    try:
        try:
            series = await client.fetch_ohlcv("BTC/USDT", "1h", limit=120)
        except PublicDataError as e:
            pytest.skip(f"公开行情不可用(网络/地区限制): {e}")
    finally:
        await client.aclose()

    assert len(series) >= 60
    now = int(time.time() * 1000)
    result = validate_series(
        series, now_ms=now, require_closed_last=False, max_staleness_ms=6 * 3_600_000
    )
    assert result.ok, result.errors

    snap = compute_snapshot(series)
    assert snap.last_close > 0
    assert snap.ma20 is not None
    assert snap.rsi14 is None or 0.0 <= snap.rsi14 <= 100.0
