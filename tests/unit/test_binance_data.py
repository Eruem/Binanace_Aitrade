"""阶段 2:CCXT 数据客户端测试(注入假交易所,不发真实请求)。"""

from __future__ import annotations

import pytest

from data.binance_data import BinancePublicData, PublicDataError

TF_MS = 3_600_000
TS0 = 1_700_000_000_000


class FakeExchange:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch_ohlcv(self, symbol, timeframe, limit=None):
        self.calls.append((symbol, timeframe, limit))
        return self.rows

    async def close(self):
        pass


def rows(n=3):
    return [
        [TS0 + i * TF_MS, 100.0 + i, 102.0 + i, 99.0 + i, 101.0 + i, 10.0 + i]
        for i in range(n)
    ]


async def test_fetch_converts_and_sorts():
    ex = FakeExchange(list(reversed(rows(4))))
    client = BinancePublicData(ex)
    series = await client.fetch_ohlcv("BTC/USDT", "1h", limit=4, now_ms=TS0 + 999)
    assert len(series) == 4
    assert series.candles[0].open_time == TS0  # 已排序
    c0 = series.candles[0]
    assert c0.open == 100.0 and c0.close == 101.0
    assert c0.close_time == TS0 + TF_MS - 1
    assert c0.source == "binance_spot_public"
    assert series.fetched_at == TS0 + 999
    assert ex.calls == [("BTC/USDT", "1h", 4)]


async def test_empty_raises():
    client = BinancePublicData(FakeExchange([]))
    with pytest.raises(PublicDataError):
        await client.fetch_ohlcv("BTC/USDT", "1h")


async def test_bad_row_raises():
    client = BinancePublicData(FakeExchange([[TS0, 1, 2]]))
    with pytest.raises(PublicDataError):
        await client.fetch_ohlcv("BTC/USDT", "1h")


async def test_exchange_error_wrapped():
    class Boom:
        async def fetch_ohlcv(self, *a, **k):
            raise RuntimeError("network down")

    client = BinancePublicData(Boom())
    with pytest.raises(PublicDataError) as ei:
        await client.fetch_ohlcv("BTC/USDT", "1h")
    assert "network down" in str(ei.value)


async def test_fetch_snapshot_series_multi_tf():
    ex = FakeExchange(rows(2))
    client = BinancePublicData(ex)
    out = await client.fetch_snapshot_series("BTC/USDT", ["15m", "1h", "4h"])
    assert set(out) == {"15m", "1h", "4h"}
    # 15m 的 close_time 按 15m 周期计算
    assert out["15m"].candles[0].close_time == TS0 + 900_000 - 1


def test_default_exchange_injects_proxy():
    """阶段 8:EXCHANGE_PROXY_URL 显式注入 ccxt(不依赖系统代理)。

    用 https_proxy 属性:异步 aiohttp 只认 http_proxy/https_proxy,
    且两者同设会冲突;本项目流量全部为 https。
    """
    ex = BinancePublicData(proxy_url="http://127.0.0.1:10808")._exchange
    assert ex.https_proxy == "http://127.0.0.1:10808"
    assert not ex.http_proxy  # 不与 https_proxy 同设(冲突)
    # 公共行情强制无凭据
    assert ex.apiKey == "" and ex.secret == ""


def test_default_exchange_no_proxy_by_default():
    ex = BinancePublicData()._exchange
    assert not getattr(ex, "https_proxy", None)


def test_injected_exchange_ignores_proxy_param():
    fake = FakeExchange([])
    client = BinancePublicData(fake, proxy_url="http://127.0.0.1:10808")
    assert client._exchange is fake
