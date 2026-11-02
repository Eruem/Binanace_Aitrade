"""数据层:行情读取、K 线模型、质量校验、指标计算。"""

from .binance_data import BinancePublicData, PublicDataError
from .binance_vision import BinanceVisionPublicData, VisionDataError
from .data_validator import ValidationResult, validate_alignment, validate_series
from .indicators import (
    IndicatorSnapshot,
    atr,
    bollinger,
    compute_snapshot,
    ema,
    macd,
    rsi,
    sma,
    volume_roc,
)
from .market_data import (
    TIMEFRAME_MS,
    Candle,
    MarketSnapshot,
    OHLCVSeries,
    timeframe_to_ms,
)

__all__ = [
    "BinancePublicData",
    "BinanceVisionPublicData",
    "Candle",
    "VisionDataError",
    "IndicatorSnapshot",
    "MarketSnapshot",
    "OHLCVSeries",
    "PublicDataError",
    "TIMEFRAME_MS",
    "ValidationResult",
    "atr",
    "bollinger",
    "compute_snapshot",
    "ema",
    "macd",
    "rsi",
    "sma",
    "timeframe_to_ms",
    "validate_alignment",
    "validate_series",
    "volume_roc",
]
