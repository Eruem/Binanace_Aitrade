"""Binance Vision 公共数据源(阶段 2 补充)。

背景:部分网络环境无法访问 api.binance.com(CCit 主机接口),但
data.binance.vision(官方历史 K 线归档镜像,纯 HTTP 文件下载)可达。
本模块作为 CCXT 之外的备选公共数据源,同样不需要任何 API Key。

- 文件地址:{base}/spot/daily/klines/{SYMBOL}/{interval}/{SYMBOL}-{interval}-{YYYY-MM-DD}.zip
- 每日 zip 内为 CSV,前 6 列为 open_time(ms),o,h,l,c,v,第 7 列 close_time(ms);
- 最新一天的归档通常次日 UTC 08:00 后才发布,默认只取到"昨天";
- 下载缺失的日期会跳过(不伪造数据),缺口由 data_validator 报告。
"""

from __future__ import annotations

import csv
import io
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from .market_data import Candle, OHLCVSeries, timeframe_to_ms

VISION_BASE = "https://data.binance.vision/data/spot/daily/klines"

Downloader = Callable[[str], Any]  # url -> bytes(async)


class VisionDataError(Exception):
    """Vision 数据源错误。"""


def symbol_to_vision(symbol: str) -> str:
    return symbol.replace("/", "").upper()


async def _http_download(url: str) -> bytes:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        raise VisionDataError(f"下载失败 {type(e).__name__}: {url}") from e
    if resp.status_code == 404:
        raise VisionDataError(f"文件不存在(可能尚未发布): {url}")
    if resp.status_code != 200:
        raise VisionDataError(f"HTTP {resp.status_code}: {url}")
    return resp.content


def _to_ms(ts: int) -> int:
    """时间戳单位归一:vision 归档为微秒(16 位),主 API 为毫秒(13 位)。"""
    return ts // 1000 if ts > 10**14 else ts


def parse_klines_csv(
    raw: bytes, *, symbol: str, timeframe: str
) -> list[Candle]:
    """解析 vision K 线 CSV(可能 gzip 或纯文本;zip 由调用方解开)。"""
    tf_ms = timeframe_to_ms(timeframe)
    text = raw.decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    out: list[Candle] = []
    for row in reader:
        if not row:
            continue
        # 首列非数字时视为表头
        try:
            open_time = _to_ms(int(row[0]))
            o, h, l, c, v = (float(row[i]) for i in range(1, 6))
            close_time = (
                _to_ms(int(row[6])) if len(row) > 6 else open_time + tf_ms - 1
            )
        except (ValueError, IndexError):
            continue
        out.append(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=open_time,
                close_time=close_time,
                open=o,
                high=h,
                low=l,
                close=c,
                volume=v,
                source="binance_vision_public",
                fetched_at=0,
            )
        )
    return out


def _open_zip(raw: bytes) -> list[Candle] | None:
    """zip bytes -> 内部唯一 CSV 的行;非 zip 返回 None(当作裸 CSV)。"""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise VisionDataError("zip 内没有 CSV")
            return zf.read(names[0])  # type: ignore[return-value]
    return None


class BinanceVisionPublicData:
    """按日归档的 Binance Spot 公共 K 线读取。"""

    def __init__(self, downloader: Downloader | None = None) -> None:
        self._download: Downloader = downloader or _http_download

    async def fetch_daily(
        self, symbol: str, timeframe: str, day: date
    ) -> list[Candle]:
        sym = symbol_to_vision(symbol)
        url = (
            f"{VISION_BASE}/{sym}/{timeframe}/"
            f"{sym}-{timeframe}-{day.isoformat()}.zip"
        )
        raw = await self._download(url)
        inner = _open_zip(raw)
        payload = inner if inner is not None else raw
        candles = parse_klines_csv(payload, symbol=symbol, timeframe=timeframe)
        if not candles:
            raise VisionDataError(f"CSV 无有效数据: {url}")
        now_ms = int(time.time() * 1000)
        return [
            c.model_copy(update={"fetched_at": now_ms}) for c in candles
        ]

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        *,
        days: int = 10,
        end_day: date | None = None,
    ) -> OHLCVSeries:
        """抓取最近 days 个自然日(默认到昨天,归档延迟)。

        缺失的日期跳过;全部缺失才报错。返回按时间排序、去重后的序列。
        """
        if days < 1:
            raise ValueError("days 必须 >= 1")
        last = end_day or (datetime.now(tz=timezone.utc).date() - timedelta(days=1))
        by_open: dict[int, Candle] = {}
        missing: list[str] = []
        for offset in range(days):
            day = last - timedelta(days=offset)
            try:
                for c in await self.fetch_daily(symbol, timeframe, day):
                    by_open[c.open_time] = c
            except VisionDataError as e:
                if "404" in str(e) or "文件不存在" in str(e):
                    missing.append(day.isoformat())
                else:
                    raise
        if not by_open:
            raise VisionDataError(
                f"{symbol} {timeframe} 最近 {days} 天归档全部缺失: {missing[:3]}..."
            )
        candles = tuple(sorted(by_open.values(), key=lambda c: c.open_time))
        return OHLCVSeries(
            symbol=symbol,
            timeframe=timeframe,
            candles=candles,
            source="binance_vision_public",
            fetched_at=int(time.time() * 1000),
        )
