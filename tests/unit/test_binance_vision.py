"""阶段 2:Vision 数据源测试(注入假下载器,不发真实请求)。"""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timezone

import pytest

from data.binance_vision import (
    BinanceVisionPublicData,
    VisionDataError,
    parse_klines_csv,
    symbol_to_vision,
)

TF_MS = 3_600_000
DAY0 = date(2026, 10, 28)


def csv_rows(day: date, n: int = 24) -> bytes:
    base = int(
        datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000
    )
    lines = []
    for i in range(n):
        ts = base + i * TF_MS
        px = 100.0 + i
        lines.append(
            f"{ts},{px},{px + 1},{px - 1},{px + 0.5},50.5,"
            f"{ts + TF_MS - 1},0,100,0,0,0"
        )
    return ("\n".join(lines) + "\n").encode()


def make_zip(csv_bytes: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("BTCUSDT-1h-2026-10-28.csv", csv_bytes)
    return buf.getvalue()


def test_symbol_to_vision():
    assert symbol_to_vision("BTC/USDT") == "BTCUSDT"


def test_parse_csv_skips_header():
    raw = b"open_time,open,high,low,close,volume,close_time\n" + csv_rows(DAY0, 2)
    candles = parse_klines_csv(raw, symbol="BTC/USDT", timeframe="1h")
    assert len(candles) == 2
    assert candles[0].open == 100.0


def test_parse_normalizes_microsecond_timestamps():
    # vision 归档实际使用微秒时间戳(16 位),须归一为毫秒
    raw = b"1793145600000000,100,101,99,100.5,10.5,1793149199999999,0,0,0,0,0\n"
    candles = parse_klines_csv(raw, symbol="BTC/USDT", timeframe="1h")
    assert candles[0].open_time == 1_793_145_600_000
    assert candles[0].close_time == 1_793_149_199_999


def test_parse_keeps_millisecond_timestamps():
    raw = b"1793145600000,100,101,99,100.5,10.5,1793149199999,0,0,0,0,0\n"
    candles = parse_klines_csv(raw, symbol="BTC/USDT", timeframe="1h")
    assert candles[0].open_time == 1_793_145_600_000


async def test_fetch_daily_zip():
    async def dl(url: str) -> bytes:
        assert "BTCUSDT/1h/BTCUSDT-1h-2026-10-28.zip" in url
        return make_zip(csv_rows(DAY0, 24))

    client = BinanceVisionPublicData(dl)
    candles = await client.fetch_daily("BTC/USDT", "1h", DAY0)
    assert len(candles) == 24
    assert candles[0].source == "binance_vision_public"


async def test_fetch_ohlcv_merges_days_and_skips_missing():
    async def dl(url: str) -> bytes:
        if "2026-10-28" in url:
            return make_zip(csv_rows(date(2026, 10, 28)))
        if "2026-10-27" in url:
            return make_zip(csv_rows(date(2026, 10, 27)))
        raise VisionDataError(f"文件不存在(可能尚未发布): {url}")

    client = BinanceVisionPublicData(dl)
    series = await client.fetch_ohlcv(
        "BTC/USDT", "1h", days=3, end_day=date(2026, 10, 28)
    )
    # 28/27 有数据,26 缺失被跳过
    assert len(series) == 48
    assert series.candles[0].open_time < series.candles[-1].open_time


async def test_fetch_ohlcv_all_missing_raises():
    async def dl(url: str) -> bytes:
        raise VisionDataError(f"文件不存在(可能尚未发布): {url}")

    client = BinanceVisionPublicData(dl)
    with pytest.raises(VisionDataError):
        await client.fetch_ohlcv("BTC/USDT", "1h", days=2, end_day=DAY0)
