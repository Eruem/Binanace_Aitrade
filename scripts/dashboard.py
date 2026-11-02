"""只读监控面板(Streamlit,阶段 9)。

启动(项目根目录):
    .venv/Scripts/python.exe -m streamlit run scripts/dashboard.py
监听地址/端口/遥测由 .streamlit/config.toml 固定(仅 127.0.0.1)。

安全边界(不可跨越):
- 只读:仅展示 var/ 下的审计/状态/日志;无任何下单、改配置入口;
- 不加载/不展示任何密钥;不 import 交易所、执行层、风控写入模块;
- 数据读取全部走 monitoring.dashboard_data(纯只读函数,离线可单测)。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitoring.dashboard_data import (  # noqa: E402
    cycle_rows,
    fmt_age,
    latest_cycle,
    loop_activity,
    read_jsonl,
    risk_checks_rows,
    risk_rows,
    summarize,
    tail_lines,
)

st.set_page_config(page_title="Agent 监控", page_icon="📊", layout="wide")

VAR = ROOT / "var"
AUDIT_PATH = VAR / "audit" / "audit.jsonl"
RISK_PATH = VAR / "risk_audit.jsonl"
LOG_PATH = VAR / "agent_loop.log"
BROKER_PATH = VAR / "paper_broker.json"
INTENTS_PATH = VAR / "order_intents.json"

_STATE_LABEL = {
    "active": "🟢 循环活跃",
    "stale": "🔴 循环已停止",
    "unknown": "⚪ 无日志",
}
_MODE_LABEL = {
    "paper": "📝 PAPER 模拟盘",
    "testnet": "🧪 TESTNET 测试网",
    "live": "🚨 LIVE 真实资金",
}


@st.cache_data(ttl=15, show_spinner=False)
def _load() -> dict:
    """读取全部展示数据(15 秒缓存,免得每次交互都打磁盘)。"""
    return {
        "cycles": read_jsonl(AUDIT_PATH),
        "risks": read_jsonl(RISK_PATH),
        "activity": loop_activity(LOG_PATH),
        "log_tail": tail_lines(LOG_PATH, 60),
    }


@st.cache_data(ttl=120, show_spinner=False)
def _mode() -> str:
    """交易模式(只取 mode 字符串;配置读取失败降级为 unknown)。"""
    try:
        from config.loader import load_config

        return str(load_config().settings.trading.mode)
    except Exception:
        return "unknown"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _render_latest(cycles: list) -> None:
    st.subheader("最新周期")
    latest = latest_cycle(cycles)
    if latest is None:
        st.info("暂无周期数据——先跑一轮 `scripts/run_agent.py`")
        return
    decision = latest.get("decision") or {}
    risk = latest.get("risk") or {}

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("结果", latest.get("final_status") or "-")
    c2.metric("动作", decision.get("action") or "-")
    c3.metric("风控", risk.get("verdict") or "-")
    amount = decision.get("amount_usdt")
    if amount is None:
        amount = risk.get("sized_amount_usdt")
    c4.metric("金额(USDT)", amount if amount is not None else "-")

    st.caption(f"decision_id:{decision.get('decision_id') or latest.get('cycle_id')}")
    if decision.get("reason"):
        with st.expander("决策理由"):
            st.write(decision["reason"])
    checks = risk_checks_rows(risk)
    if checks:
        st.markdown("**风控检查**")
        st.dataframe(checks, width="stretch", hide_index=True)
    if latest.get("llm_errors") or latest.get("data_errors"):
        st.warning(
            f"本周期 LLM 错误:{latest.get('llm_errors') or []} · "
            f"数据错误:{latest.get('data_errors') or []}"
        )


@st.fragment(run_every=30)
def _render_live() -> None:
    """数据区(30 秒自动刷新的 fragment)。"""
    data = _load()
    cycles = data["cycles"]
    stats = summarize(cycles)
    activity = data["activity"]

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric(
        "循环状态",
        _STATE_LABEL.get(activity["state"], activity["state"]),
        help=f"日志最后写入:{fmt_age(activity['age_s'])}",
    )
    c2.metric("周期总数", stats["total"])
    c3.metric("已下单周期", stats["orders"])
    c4.metric("风控否决", stats["risk_rejected"])
    c5.metric("LLM 错误", stats["llm_errors"] + stats["data_errors"])

    st.divider()
    _render_latest(cycles)

    tab_hist, tab_risk, tab_order, tab_log = st.tabs(
        ["📜 周期历史", "🛡️ 风控审计", "📦 订单与状态", "📋 循环日志"]
    )
    with tab_hist:
        rows = cycle_rows(cycles)
        if rows:
            st.dataframe(rows, width="stretch", height=420, hide_index=True)
        else:
            st.info("暂无周期数据")
    with tab_risk:
        rows = risk_rows(data["risks"])
        if rows:
            st.dataframe(rows, width="stretch", height=420, hide_index=True)
        else:
            st.info("暂无风控审计记录")
    with tab_order:
        st.markdown("**Paper Broker 状态**")
        broker = _read_json(BROKER_PATH)
        if broker is None:
            st.info("无 paper_broker.json —— 从未产生订单")
        else:
            st.json(broker)
        st.markdown("**订单意图(order_intents)**")
        intents = _read_json(INTENTS_PATH)
        if intents is None:
            st.info("无 order_intents.json —— 从未提交订单")
        else:
            st.json(intents)
    with tab_log:
        st.code("\n".join(data["log_tail"]) or "暂无日志", language="log")


def main() -> None:
    mode = _mode()
    st.title("📊 Binance AI Trading Agent · 只读监控")
    st.caption(
        f"模式:{_MODE_LABEL.get(mode, mode)} · 数据源:var/ 审计与状态文件"
        " · 只读无操作入口 · 30 秒自动刷新"
    )
    _render_live()


if __name__ == "__main__":
    main()
