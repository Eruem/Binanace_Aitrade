"""模型用量跟踪(指导书 5.2 / 12)。

- 内存中按 cycle 记录每次调用:任务、别名、provider、实际模型、
  耗时、token、状态、错误类别;
- 可选 JSONL 落盘(审计),落盘内容同样禁止出现密钥;
- 提供每轮调用计数,供 Router 实现 max_calls_per_cycle 限额。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class UsageRecord:
    cycle_id: str
    task: str
    model_alias: str
    provider: str
    model: str
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    status: str  # ok | error
    error_kind: str | None = None  # retryable | fatal | parse_error | limit
    request_id: str | None = None
    recorded_at: float = field(default_factory=time.time)
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class UsageTracker:
    def __init__(self, *, sink_path: str | Path | None = None) -> None:
        self._records: list[UsageRecord] = []
        self._lock = threading.Lock()
        self._sink = Path(sink_path) if sink_path else None
        if self._sink:
            self._sink.parent.mkdir(parents=True, exist_ok=True)

    def record(self, rec: UsageRecord) -> None:
        with self._lock:
            self._records.append(rec)
            if self._sink:
                with self._sink.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")

    def calls_in_cycle(self, cycle_id: str) -> int:
        with self._lock:
            return sum(1 for r in self._records if r.cycle_id == cycle_id)

    def records_for_cycle(self, cycle_id: str) -> list[UsageRecord]:
        with self._lock:
            return [r for r in self._records if r.cycle_id == cycle_id]

    def summary(self, cycle_id: str) -> dict[str, Any]:
        recs = self.records_for_cycle(cycle_id)
        ok = [r for r in recs if r.status == "ok"]
        tokens_in = sum(r.input_tokens or 0 for r in ok)
        tokens_out = sum(r.output_tokens or 0 for r in ok)
        return {
            "cycle_id": cycle_id,
            "total_calls": len(recs),
            "ok_calls": len(ok),
            "error_calls": len(recs) - len(ok),
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "avg_latency_ms": (
                sum(r.latency_ms for r in ok) / len(ok) if ok else 0.0
            ),
        }
