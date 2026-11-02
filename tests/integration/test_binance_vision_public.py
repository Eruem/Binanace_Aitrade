"""阶段 2 集成测试:通过 Binance Vision 归档读取真实公开 K 线。

data.binance.vision 是官方公开数据镜像,无需 API Key;
网络不可用时自动跳过。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from data.binance_vision import (
    BinanceVisionPublicData,
    VisionDataError,
    parse_klines_csv,
)
from data.data_validator import validate_series
from data.indicators import compute_snapshot
from data.market_data import timeframe_to_ms


async def test_vision_daily_1h_end_to_end():
    client = BinanceVisionPublicData()
    try:
        series = await client.fetch_ohlcv("BTC/USDT", "1h", days=7)
    except VisionDataError as e:
        pytest.skip(f"Vision 归档不可用: {e}")

    assert len(series) >= 24  # 至少一天
    now = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    tf_ms = timeframe_to_ms("1h")
    result = validate_series(
        series, now_ms=now, require_closed_last=False, max_staleness_ms=5 * 86_400_000
    )
    # 归档按日切分,跨日连续性由校验器保证;缺日会有 gap error
    assert result.ok or all("缺失" in e for e in result.errors), result.errors

    snap = compute_snapshot(series)
    assert snap.last_close > 0
    assert snap.ma20 is not None
    if snap.rsi14 is not None:
        assert 0.0 <= snap.rsi14 <= 100.0


def test_parse_real_style_csv_header():
    # vision 归档实际无表头;兼容有表头的变体
    raw = (
        b"Open time,Open,High,Low,Close,Volume,Close time\n"
        b"1761600000000,60000,61000,59500,60500,123.4,1761603599999\n"
    )
    candles = parse_klines_csv(raw, symbol="BTC/USDT", timeframe="1h")
    assert len(candles) == 1
    assert candles[0].open == 60000.0
