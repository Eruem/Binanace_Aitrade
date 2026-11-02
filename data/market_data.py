"""行情数据模型(阶段 2,指导书 7.1)。

K 线统一模型:所有数据源(CCXT/Binance)先转成 Candle 再进入校验与指标层。
时间一律 UTC 毫秒时间戳(int)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 常用周期 -> 毫秒
TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


def timeframe_to_ms(timeframe: str) -> int:
    try:
        return TIMEFRAME_MS[timeframe]
    except KeyError:
        raise ValueError(f"不支持的周期: {timeframe!r}") from None


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class Candle(BaseModel):
    """单根 K 线。字段与指导书 7.1 对齐。

    注意:本模型只做类型/基础范围校验;时序、缺口、OHLC 逻辑关系等
    质量问题由 data_validator 统一检查(一根"孤立地看合法"的 K 线
    可能在序列里是坏的)。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    timeframe: str
    open_time: int  # UTC ms,K 线开盘时刻
    close_time: int  # UTC ms,K 线收盘时刻(含)
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str = "unknown"
    fetched_at: int = 0  # UTC ms,抓取时刻

    @field_validator("timeframe")
    @classmethod
    def _tf_known(cls, v: str) -> str:
        if v not in TIMEFRAME_MS:
            raise ValueError(f"不支持的周期: {v!r}")
        return v

    @field_validator("open", "high", "low", "close", "volume")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("OHLCV 数值不允许 NaN/Infinity")
        return v


@dataclass(frozen=True)
class OHLCVSeries:
    """一个 (symbol, timeframe) 的 K 线序列 + 来源元数据。"""

    symbol: str
    timeframe: str
    candles: tuple[Candle, ...]
    source: str = "unknown"
    fetched_at: int = 0

    def __len__(self) -> int:
        return len(self.candles)

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.candles]

    @property
    def volumes(self) -> list[float]:
        return [c.volume for c in self.candles]

    @property
    def last(self) -> Candle | None:
        return self.candles[-1] if self.candles else None


@dataclass(frozen=True)
class MarketSnapshot:
    """一次分析周期使用的多周期行情快照(阶段 3 的 Agent 输入)。"""

    symbol: str
    series: dict[str, OHLCVSeries] = field(default_factory=dict)
    fetched_at: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    def get(self, timeframe: str) -> OHLCVSeries | None:
        return self.series.get(timeframe)
