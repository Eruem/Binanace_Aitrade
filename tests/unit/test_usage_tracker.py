"""阶段 1:用量跟踪测试。"""

from __future__ import annotations

import json

from llm.usage_tracker import UsageRecord, UsageTracker


def _rec(cycle: str, *, status: str = "ok", alias: str = "fast") -> UsageRecord:
    return UsageRecord(
        cycle_id=cycle,
        task="market_summary",
        model_alias=alias,
        provider="primary",
        model="m",
        latency_ms=12.0,
        input_tokens=10,
        output_tokens=5,
        status=status,
        error_kind=None if status == "ok" else "retryable",
    )


def test_counts_per_cycle():
    t = UsageTracker()
    t.record(_rec("c1"))
    t.record(_rec("c1"))
    t.record(_rec("c2"))
    assert t.calls_in_cycle("c1") == 2
    assert t.calls_in_cycle("c2") == 1
    assert t.calls_in_cycle("c3") == 0


def test_summary():
    t = UsageTracker()
    t.record(_rec("c1"))
    t.record(_rec("c1", status="error"))
    s = t.summary("c1")
    assert s["total_calls"] == 2
    assert s["ok_calls"] == 1
    assert s["error_calls"] == 1
    assert s["input_tokens"] == 10


def test_jsonl_sink(tmp_path):
    sink = tmp_path / "usage.jsonl"
    t = UsageTracker(sink_path=sink)
    t.record(_rec("c1"))
    lines = sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["cycle_id"] == "c1"
    assert "record_id" in data
