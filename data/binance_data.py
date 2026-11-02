"""Binance Spot 公共行情读取(阶段 2,指导书 7.1)。

- 只读公开行情,不需要 API Key;
- 通过 CCXT 获取 OHLCV 并转换为 Candle/OHLCVSeries;
- 不持有任何交易所凭据,不创建下单客户端;
- 交易所对象可注入(测试用假对象,不发真实请求)。
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from .market_data import Candle, OHLCVSeries, timeframe_to_ms


class PublicDataError(Exception):
    """行情读取失败。"""


def _default_exchange(proxy_url: str | None = None) -> Any:
    try:
        import ccxt.async_support as ccxt_async
    except ImportError as e:  # pragma: no cover
        raise PublicDataError(
            "未安装 ccxt;阶段 2 依赖: pip install -r requirements.txt"
        ) from e
    config: dict[str, Any] = {
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    }
    ex = ccxt_async.binance(config)
    if proxy_url:
        # CCXT 默认 trust_env=False,不读系统/环境代理;被墙网络显式注入。
        # 用 https_proxy 属性(异步 aiohttp 只认 http_proxy/https_proxy,
        # 且两者同设会冲突;本项目流量全部为 https)
        ex.https_proxy = proxy_url
    # 公共行情:强制无凭据,避免误带测试网/正式网密钥
    ex.apiKey = ""
    ex.secret = ""
    return ex


class BinancePublicData:
    """Binance Spot 公共 K 线客户端。"""

    def __init__(
        self, exchange: Any = None, *, proxy_url: str | None = None
    ) -> None:
        self._exchange = (
            exchange if exchange is not None else _default_exchange(proxy_url)
        )
        self._owns_exchange = exchange is None

    async def aclose(self) -> None:
        if self._owns_exchange:
            await self._exchange.close()

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        *,
        limit: int = 200,
        now_ms: int | None = None,
    ) -> OHLCVSeries:
        """抓取并转换为 OHLCVSeries。

        Args:
            symbol: 交易对,如 "BTC/USDT"。
            timeframe: "15m" / "1h" / "4h" 等。
            limit: K 线数量。
            now_ms: 当前时间(毫秒),用于记录 fetched_at;默认系统时间。
        """
        try:
            raw: Sequence[Sequence[float]] = await self._exchange.fetch_ohlcv(
                symbol, timeframe, limit=limit
            )
        except Exception as e:
            raise PublicDataError(
                f"fetch_ohlcv 失败 {symbol} {timeframe}: {type(e).__name__}: {e}"
            ) from e

        if not raw:
            raise PublicDataError(f"fetch_ohlcv 返回空: {symbol} {timeframe}")

        tf_ms = timeframe_to_ms(timeframe)
        fetched_at = now_ms if now_ms is not None else int(time.time() * 1000)
        candles: list[Candle] = []
        for row in raw:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                raise PublicDataError(f"OHLCV 行格式非法: {row!r}")
            ts, o, h, l, c, v = row[:6]
            candles.append(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=int(ts),
                    close_time=int(ts) + tf_ms - 1,
                    open=float(o),
                    high=float(h),
                    low=float(l),
                    close=float(c),
                    volume=float(v),
                    source="binance_spot_public",
                    fetched_at=fetched_at,
                )
            )

        candles.sort(key=lambda x: x.open_time)
        return OHLCVSeries(
            symbol=symbol,
            timeframe=timeframe,
            candles=tuple(candles),
            source="binance_spot_public",
            fetched_at=fetched_at,
        )

    async def fetch_snapshot_series(
        self, symbol: str, timeframes: Sequence[str], *, limit: int = 200
    ) -> dict[str, OHLCVSeries]:
        """按顺序抓取多个周期,返回 {timeframe: series}。"""
        out: dict[str, OHLCVSeries] = {}
        for tf in timeframes:
            out[tf] = await self.fetch_ohlcv(symbol, tf, limit=limit)
        return out
