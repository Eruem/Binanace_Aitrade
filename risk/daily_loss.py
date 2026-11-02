"""日亏损限制(阶段 4,指导书 8.2)。

- 按 UTC 日统计已实现盈亏(手续费已计入成交盈亏,由执行层传入净额);
- 当日亏损达到 max_daily_loss_pct × 权益 → 禁止新开仓(BUY);
  平仓/减仓不受限;
- 状态持久化到本地 JSON,重启不丢当日统计;
- 权益快照由调用方提供,本模块只做纯统计与阈值判断。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


def utc_day(ms: int | None = None) -> str:
    if ms is None:
        return datetime.now(tz=timezone.utc).date().isoformat()
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


@dataclass(frozen=True)
class DailyLossStatus:
    day: str
    realized_pnl_usdt: Decimal
    equity_usdt: Decimal
    limit_usdt: Decimal  # 正数表示允许亏损额度
    breached: bool


class DailyLossTracker:
    def __init__(self, state_path: str | Path | None = None) -> None:
        self._path = Path(state_path) if state_path else None
        self._lock = threading.Lock()
        self._days: dict[str, Decimal] = {}
        if self._path and self._path.exists():
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
            self._days = {k: Decimal(str(v)) for k, v in data.items()}
        except (OSError, ValueError):
            # 损坏时按 0 处理会放松风控,改为留空但记录失败;这里选择
            # 保守策略:抛错让上层拒绝交易,而不是假装没有亏损。
            raise DailyLossStateError("日亏损状态文件损坏,拒绝启动交易循环")

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({k: str(v) for k, v in self._days.items()}), encoding="utf-8"
        )

    def record(self, realized_pnl_usdt: Decimal, *, day: str | None = None) -> None:
        """记入一笔已实现盈亏(盈利为正,亏损为负)。"""
        key = day or utc_day()
        with self._lock:
            self._days[key] = self._days.get(key, Decimal("0")) + realized_pnl_usdt
            self._save()

    def realized_today(self, *, day: str | None = None) -> Decimal:
        key = day or utc_day()
        with self._lock:
            return self._days.get(key, Decimal("0"))

    def status(
        self, *, equity_usdt: Decimal, max_daily_loss_pct: float, day: str | None = None
    ) -> DailyLossStatus:
        key = day or utc_day()
        realized = self.realized_today(day=key)
        limit = equity_usdt * Decimal(str(max_daily_loss_pct))
        breached = realized <= -limit if limit > 0 else True
        return DailyLossStatus(
            day=key,
            realized_pnl_usdt=realized,
            equity_usdt=equity_usdt,
            limit_usdt=limit,
            breached=breached,
        )


class DailyLossStateError(Exception):
    """日亏损状态不可用(损坏等)。上层必须当作风控失败处理。"""
