"""内存指标收集器(阶段 7,指导书 12)。

覆盖监控清单:模型成功/失败/延迟、每轮调用数与 token、行情数据延迟、
Agent 失败、风控拒绝、订单/成交状态、账户余额/持仓、Kill Switch 状态。

进程内计数 + 快照导出;不引入外部依赖(Prometheus 导出留待后续)。
线程安全(周期循环与对账可能来自不同线程)。
"""

from __future__ import annotations

import statistics
import threading
from typing import Any


class MetricsCollector:
    """计数器/仪表/延迟样本的线程安全收集器。"""

    def __init__(self, *, max_samples: int = 1000):
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._samples: dict[str, list[float]] = {}
        self._max_samples = max_samples

    # ---------------------------------------------------------------- 原语

    def incr(self, name: str, value: int = 1, **labels: Any) -> None:
        key = _key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value

    def gauge(self, name: str, value: float, **labels: Any) -> None:
        with self._lock:
            self._gauges[_key(name, labels)] = value

    def observe(self, name: str, value: float, **labels: Any) -> None:
        key = _key(name, labels)
        with self._lock:
            bucket = self._samples.setdefault(key, [])
            bucket.append(value)
            if len(bucket) > self._max_samples:
                del bucket[: len(bucket) - self._max_samples]

    # ------------------------------------------------------- §12 监控清单

    def record_model_call(
        self,
        *,
        alias: str,
        ok: bool,
        latency_ms: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        self.incr("llm_calls_total", model_alias=alias, status="ok" if ok else "error")
        if latency_ms is not None:
            self.observe("llm_latency_ms", latency_ms, model_alias=alias)
        if input_tokens:
            self.incr("llm_input_tokens_total", value=input_tokens, model_alias=alias)
        if output_tokens:
            self.incr("llm_output_tokens_total", value=output_tokens, model_alias=alias)

    def record_market_latency(self, symbol: str, timeframe: str, latency_ms: float) -> None:
        self.observe("market_data_latency_ms", latency_ms, symbol=symbol, timeframe=timeframe)

    def record_agent_failure(self, agent: str) -> None:
        self.incr("agent_failures_total", agent=agent)

    def record_risk_rejection(self, decision_id: str, *, rule: str | None = None) -> None:
        self.incr("risk_rejections_total", rule=rule or "unknown")

    def record_risk_verdict(self, verdict: str) -> None:
        self.incr("risk_verdicts_total", verdict=verdict)

    def record_order_status(self, status: str, *, side: str | None = None) -> None:
        self.incr("orders_total", status=status, side=side or "unknown")

    def record_trade(self, *, side: str, symbol: str) -> None:
        self.incr("trades_total", side=side, symbol=symbol)

    def record_account(self, equity_usdt: float, available_usdt: float) -> None:
        self.gauge("account_equity_usdt", equity_usdt)
        self.gauge("account_available_usdt", available_usdt)

    def record_kill_switch(self, engaged: bool) -> None:
        self.gauge("kill_switch_engaged", 1.0 if engaged else 0.0)

    def record_cycle(self, final_status: str) -> None:
        self.incr("cycles_total", final_status=final_status)

    # ---------------------------------------------------------------- 导出

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            latency = {}
            for key, values in self._samples.items():
                latency[key] = {
                    "count": len(values),
                    "avg": round(statistics.fmean(values), 3) if values else None,
                    "p95": round(_percentile(values, 0.95), 3) if values else None,
                    "max": round(max(values), 3) if values else None,
                }
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "latencies": latency,
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._samples.clear()


def _key(name: str, labels: dict[str, Any]) -> str:
    if not labels:
        return name
    parts = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{parts}}}"


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = min(int(q * len(ordered)), len(ordered) - 1)
    return ordered[idx]


# 进程级默认实例(主流程与脚本共用;测试可自建)
collector = MetricsCollector()
