"""监控面板数据层(阶段 9,只读)。

安全边界(不可跨越):
- 只读取 var/ 下的审计/状态/日志文件;绝不写入、绝不触碰交易所、
  绝不加载或展示任何密钥(不读 .env、不读配置敏感字段);
- 本模块不依赖 streamlit、不依赖网络,可完全离线单测。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# cycle_id 形如 drive-<epoch_ms>-BTCUSDT;decision_id 前缀 cycle-
_EPOCH_MS_RE = re.compile(r"(\d{13})")


def read_jsonl(path: str | Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    """逐行读 JSONL;坏行跳过、文件缺失返回空(读取失败绝不影响展示)。

    limit 只保留末尾 N 条(日志追加写入,末尾 = 最新)。
    """
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        return []
    if limit is not None and len(rows) > limit:
        rows = rows[-limit:]
    return rows


def tail_lines(path: str | Path, n: int = 50) -> list[str]:
    """日志末尾 n 行;文件缺失/不可读返回空列表。"""
    p = Path(path)
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    return [ln.rstrip("\n") for ln in lines[-n:]]


def loop_activity(
    log_path: str | Path,
    *,
    interval_s: int = 1800,  # 与 docker-compose 的 --interval 保持一致
    grace_s: int = 240,
    now: float | None = None,
) -> dict[str, Any]:
    """按循环日志 mtime 判断活动状态(只给"迹象",不下硬结论)。

    - active:距上次写入 ≤ interval + grace(grace = 单轮耗时余量);
    - stale:超过该窗口(循环多半已停);
    - unknown:日志文件不存在。
    """
    p = Path(log_path)
    ts = now if now is not None else time.time()
    if not p.exists():
        return {"state": "unknown", "age_s": None, "last_activity": None}
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {"state": "unknown", "age_s": None, "last_activity": None}
    age = max(0.0, ts - mtime)
    state = "active" if age <= interval_s + grace_s else "stale"
    return {"state": state, "age_s": int(age), "last_activity": mtime}


def summarize(cycles: list[dict[str, Any]]) -> dict[str, int]:
    """周期汇总 KPI(面板顶部指标行)。"""
    total = len(cycles)
    hold = sum(
        1 for c in cycles if str(c.get("final_status") or "").startswith("hold")
    )
    orders = sum(1 for c in cycles if c.get("order"))
    risk_rejected = sum(
        1
        for c in cycles
        if ((c.get("risk") or {}).get("verdict")) == "REJECTED"
    )
    llm_errors = sum(len(c.get("llm_errors") or []) for c in cycles)
    data_errors = sum(len(c.get("data_errors") or []) for c in cycles)
    return {
        "total": total,
        "hold": hold,
        "orders": orders,
        "risk_rejected": risk_rejected,
        "llm_errors": llm_errors,
        "data_errors": data_errors,
    }


def latest_cycle(cycles: list[dict[str, Any]]) -> dict[str, Any] | None:
    """最新周期(日志追加写,末行 = 最新)。"""
    return cycles[-1] if cycles else None


def cycle_epoch_ms(cycle_id: str | None) -> int | None:
    """从 cycle_id 提取 13 位毫秒时间戳;提取不到返回 None。"""
    if not cycle_id:
        return None
    m = _EPOCH_MS_RE.search(str(cycle_id))
    return int(m.group(1)) if m else None


def fmt_ms(ms: int | None) -> str:
    """毫秒时间戳 → 本地时间字符串;无效返回占位符。"""
    if not ms:
        return "-"
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "-"


def fmt_age(seconds: int | None) -> str:
    """秒数 → 人话("3 分 20 秒前");无效返回占位符。"""
    if seconds is None:
        return "-"
    s = int(seconds)
    if s < 60:
        return f"{s} 秒前"
    if s < 3600:
        return f"{s // 60} 分 {s % 60} 秒前"
    return f"{s // 3600} 小时 {(s % 3600) // 60} 分前"


def cycle_rows(cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """周期历史表行(最新在前;中文列名供面板直接展示)。"""
    rows: list[dict[str, Any]] = []
    for c in reversed(cycles):
        decision = c.get("decision") or {}
        risk = c.get("risk") or {}
        amount = decision.get("amount_usdt")
        if amount is None:
            amount = risk.get("sized_amount_usdt")
        rows.append(
            {
                "时间": fmt_ms(cycle_epoch_ms(c.get("cycle_id"))),
                "交易对": c.get("symbol") or "-",
                "结果": c.get("final_status") or "-",
                "动作": decision.get("action") or "-",
                "策略": c.get("strategy") or decision.get("strategy") or "-",
                "置信度": decision.get("confidence"),
                "风控": risk.get("verdict") or "-",
                "金额USDT": amount,
                "决策ID": decision.get("decision_id") or c.get("cycle_id") or "-",
            }
        )
    return rows


def risk_rows(risks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """风控审计表行(最新在前)。"""
    rows: list[dict[str, Any]] = []
    for r in reversed(risks):
        checks = r.get("checks") or []
        passed = sum(1 for c in checks if c.get("passed"))
        rows.append(
            {
                "时间": fmt_ms(r.get("checked_at")),
                "决策ID": r.get("decision_id") or "-",
                "动作": r.get("action") or "-",
                "结论": r.get("verdict") or "-",
                "金额USDT": r.get("sized_amount_usdt"),
                "检查": f"{passed}/{len(checks)}",
                "原因": "、".join(str(x) for x in (r.get("reasons") or [])) or "-",
            }
        )
    return rows


def risk_checks_rows(risk: dict[str, Any] | None) -> list[dict[str, Any]]:
    """单条风控结果的检查明细行。"""
    if not risk:
        return []
    return [
        {
            "检查": c.get("rule") or "-",
            "结果": "✅" if c.get("passed") else "❌",
            "说明": c.get("detail") or "-",
        }
        for c in (risk.get("checks") or [])
    ]
