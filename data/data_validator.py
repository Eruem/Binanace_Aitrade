"""行情数据质量校验(阶段 2,指导书 7.2)。

必须检查:
1. K 线缺失(按周期推算缺口);
2. 时间戳重复;
3. 时间顺序正确(严格递增);
4. OHLC 关系合理(high>=open/close>=low,high>=low,volume>=0);
5. NaN / Infinity;
6. 最新 K 线是否已收盘(策略默认只用已收盘 K 线);
7. 多周期数据是否对齐;
8. 数据是否过期。

发现异常时:上层必须拒绝本轮新开仓并记录原因;不允许 AI 猜测补齐。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .market_data import OHLCVSeries, timeframe_to_ms


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def validate_series(
    series: OHLCVSeries,
    *,
    now_ms: int,
    max_staleness_ms: int | None = None,
    require_closed_last: bool = True,
) -> ValidationResult:
    """校验单周期序列。默认要求最后一根 K 线已收盘。"""
    r = ValidationResult(ok=True)
    tf_ms = timeframe_to_ms(series.timeframe)
    candles = series.candles

    if not candles:
        r.fail("序列为空")
        return r

    # 1-3:顺序 / 重复 / 缺口
    prev_open: int | None = None
    for i, c in enumerate(candles):
        if prev_open is not None:
            if c.open_time == prev_open:
                r.fail(f"第 {i} 根 K 线时间戳重复: open_time={c.open_time}")
            elif c.open_time < prev_open:
                r.fail(f"第 {i} 根 K 线时间倒序: {c.open_time} < {prev_open}")
            elif c.open_time - prev_open > tf_ms:
                gap = (c.open_time - prev_open) // tf_ms - 1
                r.fail(
                    f"第 {i} 根 K 线前缺失 {gap} 根"
                    f"(open_time {prev_open} -> {c.open_time})"
                )
        prev_open = c.open_time

    # 4-5:OHLC 逻辑与有限性(双保险;Candle 构造时已查 NaN)
    for i, c in enumerate(candles):
        if c.high < c.low:
            r.fail(f"第 {i} 根 K 线 high < low")
        if c.high < c.open or c.high < c.close:
            r.fail(f"第 {i} 根 K 线 high 小于 open/close")
        if c.low > c.open or c.low > c.close:
            r.fail(f"第 {i} 根 K 线 low 大于 open/close")
        if c.volume < 0:
            r.fail(f"第 {i} 根 K 线 volume < 0")
        for name in ("open", "high", "low", "close", "volume"):
            v = getattr(c, name)
            if v != v or v in (float("inf"), float("-inf")):
                r.fail(f"第 {i} 根 K 线 {name} 含 NaN/Infinity")

    # 单根 K 线时长应与周期一致(容差:交易所惯例 close_time 可能多 1ms)
    for i, c in enumerate(candles):
        dur = c.close_time - c.open_time
        if dur < tf_ms - 1 or dur > tf_ms + 1:
            r.warn(f"第 {i} 根 K 线时长 {dur}ms 与周期 {tf_ms}ms 不符")

    last = candles[-1]

    # 6:最后一根是否已收盘
    if require_closed_last and last.close_time > now_ms:
        r.fail(
            f"最新 K 线尚未收盘(close_time={last.close_time} > now={now_ms});"
            "策略默认只使用已收盘 K 线"
        )

    # 8:过期(以最后一根已收盘 K 线的 close_time 衡量)
    if max_staleness_ms is not None:
        last_closed = None
        for c in reversed(candles):
            if c.close_time <= now_ms:
                last_closed = c
                break
        if last_closed is None:
            r.fail("没有任何已收盘 K 线,无法判断新鲜度")
        else:
            age = now_ms - last_closed.close_time
            if age > max_staleness_ms:
                r.fail(
                    f"数据过期: 最后已收盘 K 线距今 {age}ms > 允许 {max_staleness_ms}ms"
                )

    return r


def validate_alignment(
    series_list: list[OHLCVSeries], *, now_ms: int
) -> ValidationResult:
    """多周期对齐校验。

    规则:
    - 每个序列先各自通过单序列校验(已收盘、时序);
    - 高周期的最后已收盘时间必须 <= 低周期的最后已收盘时间
      (高周期收盘蕴含低周期已收盘);
    - 低周期的最后已收盘时间必须落在高周期边界内,即低周期的最后一根
      已收盘 K 线属于高周期最后一根已收盘 K 线覆盖的区间或更早。
    """
    r = ValidationResult(ok=True)
    if len(series_list) < 2:
        return r

    last_closed: dict[str, int] = {}
    for s in series_list:
        base = validate_series(s, now_ms=now_ms, require_closed_last=True)
        if not base.ok:
            for e in base.errors:
                r.fail(f"[{s.timeframe}] {e}")
        closed = [c for c in s.candles if c.close_time <= now_ms]
        if closed:
            last_closed[s.timeframe] = closed[-1].close_time

    if len(last_closed) < 2:
        return r

    items = sorted(
        ((timeframe_to_ms(tf), tf, ct) for tf, ct in last_closed.items())
    )
    # items 从小周期到大周期
    for (small_ms, small_tf, small_ct), (big_ms, big_tf, big_ct) in zip(
        items, items[1:]
    ):
        if big_ct > small_ct:
            r.fail(
                f"多周期不一致: {big_tf} 最后收盘 {big_ct} 晚于 {small_tf} 的 {small_ct}"
            )
            continue
        # small 的最后收盘应属于 big 最后一根已收盘 K 线覆盖的区间
        big_open = big_ct - big_ms
        if not (big_open <= small_ct <= big_ct):
            r.fail(
                f"多周期不齐: {small_tf} 最后收盘 {small_ct} 不在 {big_tf} "
                f"最后一根已收盘区间 [{big_open}, {big_ct}] 内"
            )

    return r
