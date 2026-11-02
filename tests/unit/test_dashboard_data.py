"""单元测试:监控面板数据层(纯只读,离线)。"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from monitoring import dashboard_data
from monitoring.dashboard_data import (
    cycle_epoch_ms,
    cycle_rows,
    fmt_age,
    fmt_ms,
    latest_cycle,
    loop_activity,
    read_jsonl,
    risk_checks_rows,
    risk_rows,
    summarize,
    tail_lines,
)

ROOT = Path(__file__).resolve().parents[2]


def _write_jsonl(path: Path, rows: list) -> Path:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    return path


def _cycle(i: int, *, status: str = "hold_no_trade", order=None, llm_errors=None):
    return {
        "cycle_id": f"drive-{1793543416536 + i}-BTCUSDT",
        "symbol": "BTC/USDT",
        "final_status": status,
        "decision": {"decision_id": f"cycle-drive-{i}", "action": "BUY" if order else "HOLD"},
        "risk": {"verdict": "REJECTED" if status == "risk_rejected" else "NO_ORDER",
                 "checks": [{"rule": "hold", "passed": True, "detail": "d"}]},
        "order": order,
        "llm_errors": llm_errors or [],
        "data_errors": [],
    }


# ------------------------------------------------------------------ read_jsonl


def test_read_jsonl_parses_rows(tmp_path):
    p = _write_jsonl(tmp_path / "a.jsonl", [{"x": 1}, {"x": 2}])
    assert read_jsonl(p) == [{"x": 1}, {"x": 2}]


def test_read_jsonl_skips_corrupt_lines(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_text('{"ok": 1}\nnot-json\n\n{"ok": 2}\n[1,2]\n', encoding="utf-8")
    rows = read_jsonl(p)
    assert [r["ok"] for r in rows] == [1, 2]  # 坏行与非 dict 行都跳过


def test_read_jsonl_missing_file_returns_empty(tmp_path):
    assert read_jsonl(tmp_path / "nope.jsonl") == []


def test_read_jsonl_limit_keeps_tail(tmp_path):
    p = _write_jsonl(tmp_path / "a.jsonl", [{"i": i} for i in range(5)])
    rows = read_jsonl(p, limit=2)
    assert [r["i"] for r in rows] == [3, 4]


# ------------------------------------------------------------------ tail_lines


def test_tail_lines_returns_last_n(tmp_path):
    p = tmp_path / "log.txt"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    assert tail_lines(p, 2) == ["b", "c"]


def test_tail_lines_missing_file(tmp_path):
    assert tail_lines(tmp_path / "nope.log") == []


# ------------------------------------------------------------------ loop_activity


def test_loop_activity_active_when_recent(tmp_path):
    log = tmp_path / "loop.log"
    log.write_text("x", encoding="utf-8")
    now = log.stat().st_mtime + 60  # 60 秒前写入,间隔 900 → 活跃
    act = loop_activity(log, interval_s=900, now=now)
    assert act["state"] == "active"
    assert act["age_s"] == 60


def test_loop_activity_stale_when_old(tmp_path):
    log = tmp_path / "loop.log"
    log.write_text("x", encoding="utf-8")
    now = log.stat().st_mtime + 900 + 240 + 1  # 超过 interval + grace
    assert loop_activity(log, interval_s=900, now=now)["state"] == "stale"


def test_loop_activity_unknown_when_missing(tmp_path):
    act = loop_activity(tmp_path / "nope.log")
    assert act["state"] == "unknown"
    assert act["age_s"] is None


# ------------------------------------------------------------------ 汇总与展示行


def test_summarize_counts_all_kpis():
    cycles = [
        _cycle(1),  # hold
        _cycle(2, status="executed", order={"side": "BUY"}),  # 订单
        _cycle(3, status="risk_rejected"),  # 风控否决
        _cycle(4, llm_errors=["timeout"]),  # llm 错误
    ]
    s = summarize(cycles)
    assert s == {
        "total": 4, "hold": 2, "orders": 1, "risk_rejected": 1,
        "llm_errors": 1, "data_errors": 0,
    }


def test_summarize_empty():
    assert summarize([])["total"] == 0


def test_latest_cycle_is_last_line():
    cycles = [_cycle(1), _cycle(2)]
    assert latest_cycle(cycles)["cycle_id"].endswith("-BTCUSDT")
    assert latest_cycle([]) is None
    assert latest_cycle(cycles)["cycle_id"] == _cycle(2)["cycle_id"]


def test_cycle_epoch_extraction():
    assert cycle_epoch_ms("drive-1793543416536-BTCUSDT") == 1793543416536
    assert cycle_epoch_ms("cycle-drive-1793543416536") == 1793543416536
    assert cycle_epoch_ms(None) is None
    assert cycle_epoch_ms("no-timestamp") is None


def test_fmt_helpers():
    assert fmt_ms(None) == "-"
    assert fmt_ms(1793543416536).startswith("20")  # 本地时间可解析
    assert fmt_age(None) == "-"
    assert fmt_age(30) == "30 秒前"
    assert "分" in fmt_age(200)
    assert "小时" in fmt_age(7300)


def test_cycle_rows_newest_first_with_chinese_columns():
    rows = cycle_rows([_cycle(1), _cycle(2)])
    assert len(rows) == 2
    # 最新在前
    assert rows[0]["决策ID"] == "cycle-drive-2"
    assert {"时间", "交易对", "结果", "动作", "风控", "决策ID"} <= set(rows[0])


def test_risk_rows_newest_first():
    risks = [
        {"decision_id": "a", "action": "HOLD", "verdict": "NO_ORDER",
         "checked_at": 1793543416582,
         "checks": [{"rule": "hold", "passed": True}]},
        {"decision_id": "b", "action": "SELL", "verdict": "REJECTED",
         "checked_at": 1793543516582, "reasons": ["超限"]},
    ]
    rows = risk_rows(risks)
    assert rows[0]["决策ID"] == "b"
    assert rows[0]["检查"] == "0/0"  # 无 checks → 0/0 而非除零
    assert rows[1]["检查"] == "1/1"
    assert rows[0]["原因"] == "超限"


def test_risk_checks_rows_marks_pass_and_fail():
    rows = risk_checks_rows(
        {"checks": [
            {"rule": "a", "passed": True, "detail": "ok"},
            {"rule": "b", "passed": False, "detail": "no"},
        ]}
    )
    assert rows[0]["结果"] == "✅"
    assert rows[1]["结果"] == "❌"
    assert risk_checks_rows(None) == []


# ------------------------------------------------------------------ 安全边界


def test_dashboard_data_module_is_read_only_and_keyless():
    """数据层:无环境变量读取、无密钥、无写入 API。"""
    src = inspect.getsource(dashboard_data)
    for banned in ("getenv", "environ", "dotenv", "write_text", "api_key", "api_secret"):
        assert banned not in src, banned


def test_dashboard_script_read_only_surface():
    """面板脚本:只读展示,无交易/配置/密钥相关接口。"""
    src = (ROOT / "scripts" / "dashboard.py").read_text(encoding="utf-8")
    for banned in (
        "build_broker", "submit", "create_order", "execute",
        "getenv", "environ", "api_key", "api_secret", "TELEGRAM",
    ):
        assert banned not in src, banned
    assert "monitoring.dashboard_data" in src  # 读取全部走只读数据层


def test_streamlit_bound_to_localhost():
    """面板只监听本机,不对外网开放;遥测关闭。"""
    cfg = (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    assert 'address = "127.0.0.1"' in cfg
    assert "gatherUsageStats = false" in cfg


def test_dashboard_app_renders_via_apptest():
    """整页冒烟:AppTest 离线渲染无异常,标题/指标/标签页齐全。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "scripts" / "dashboard.py"), default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("只读监控" in t.value for t in at.title)
    labels = {m.label for m in at.metric}
    assert {"循环状态", "周期总数", "已下单周期"} <= labels
    assert len(at.tabs) == 4
