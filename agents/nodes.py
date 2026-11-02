"""LangGraph 节点实现(阶段 3,指导书 6.3)。

安全规则:
- 任何数据错误或模型失败 → HOLD / NO_TRADE,绝不产生 BUY/SELL;
- LLM 输出必须经 Pydantic Schema 校验(未知字段即拒绝);
- decision_id / created_at / symbol 由系统覆写,不信任模型自述;
- 分析节点不接触交易所;执行只经 execute_node(阶段 5)走注入的
  OrderManager/Broker 接口,且必须先有风控放行。

依赖全部注入:ModelRouter、行情客户端、时钟、审计落盘路径。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from llm.complexity import ComplexityInput
from llm.response_parser import parse_llm_json
from llm.router import ModelRouter
from llm.schemas import SafeFailure, TaskType

from data.data_validator import validate_series
from data.indicators import IndicatorSnapshot, compute_snapshot
from data.market_data import MarketSnapshot, OHLCVSeries

from .schemas import (
    Action,
    AgentRunMeta,
    MarketAnalysis,
    StrategyName,
    StrategyProposal,
    TradingDecision,
)
from .state import AgentState

if TYPE_CHECKING:  # 避免 risk/ exchange <-> agents 循环导入
    from database.repository import Recorder
    from exchange.order_manager import OrderManager
    from monitoring.metrics import MetricsCollector
    from monitoring.telegram import TelegramNotifier
    from risk.engine import AccountSnapshot, RiskEngine

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# HOLD 决策的默认信号有效期:1 小时
DEFAULT_HOLD_TTL_MS = 3_600_000


@dataclass
class NodeDeps:
    """节点运行依赖(测试注入假对象,不发真实请求)。"""

    router: ModelRouter
    fetch_series: Callable[[str, list[str]], Awaitable[dict[str, OHLCVSeries]]]
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000)
    audit_path: Path | None = None
    max_staleness_ms: int = 6 * 3_600_000  # 1h K 线超过 6 小时视为过期
    # 阶段 4:注入风控引擎与账户快照来源;为 None 时不挂载 risk_node
    risk_engine: "RiskEngine | None" = None
    get_account: Callable[[], Awaitable["AccountSnapshot"]] | None = None
    # 阶段 5:注入订单管理器;仅当风控引擎与 get_account 同时存在时才挂载
    # execute_node(无风控放行绝不执行,fail-safe)
    order_manager: "OrderManager | None" = None
    # 阶段 7:入库(可选,失败降级为仅 JSONL)、指标、Telegram 通知(旁路)
    recorder: "Recorder | None" = None
    metrics: "MetricsCollector | None" = None
    notifier: "TelegramNotifier | None" = None


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def summarize_snapshot(
    snapshot: MarketSnapshot,
    indicators: dict[str, IndicatorSnapshot],
    *,
    max_closes: int = 30,
) -> str:
    """把行情快照压缩为给 LLM 的确定性文本摘要。"""
    lines: list[str] = [f"symbol: {snapshot.symbol}"]
    for tf, series in snapshot.series.items():
        closes = series.closes[-max_closes:]
        line = (
            f"[{tf}] candles={len(series)} last_close={series.last.close if series.last else 'n/a'} "
            f"recent_closes={closes}"
        )
        snap = indicators.get(tf)
        if snap is not None:
            line += (
                f" | ma20={snap.ma20} ma60={snap.ma60} rsi14={snap.rsi14} "
                f"macd(dif={snap.macd_dif}, dea={snap.macd_dea}, hist={snap.macd_hist}) "
                f"atr14={snap.atr14} bb(upper={snap.bb_upper}, mid={snap.bb_mid}, lower={snap.bb_lower}) "
                f"volume_ma20={snap.volume_ma20} volume_roc1={snap.volume_roc1}"
            )
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------

async def market_data_node(state: AgentState, deps: NodeDeps) -> dict[str, Any]:
    """抓取多周期行情 -> 校验 -> 指标。任何 error 记入 data_errors。"""
    symbol = state["symbol"]
    timeframes = state["timeframes"]
    errors: list[str] = []
    snapshot: MarketSnapshot | None = None
    indicators: dict[str, IndicatorSnapshot] = {}

    try:
        series_map = await deps.fetch_series(symbol, timeframes)
    except Exception as e:
        errors.append(f"行情抓取失败: {type(e).__name__}: {e}")
        series_map = {}

    now = deps.now_ms()
    valid_series: dict[str, OHLCVSeries] = {}
    for tf, series in series_map.items():
        result = validate_series(
            series,
            now_ms=now,
            max_staleness_ms=deps.max_staleness_ms,
            require_closed_last=False,
        )
        if not result.ok:
            errors.extend(f"[{tf}] {e}" for e in result.errors)
            continue
        valid_series[tf] = series
        try:
            indicators[tf] = compute_snapshot(series)
        except ValueError as e:
            errors.append(f"[{tf}] 指标计算失败: {e}")

    if valid_series and not errors:
        snapshot = MarketSnapshot(
            symbol=symbol, series=valid_series, fetched_at=now
        )

    return {
        "market_snapshot": snapshot,
        "indicator_summaries": indicators,
        "data_errors": errors,
    }


async def market_agent_node(state: AgentState, deps: NodeDeps) -> dict[str, Any]:
    """Market Agent:分析同一份不可变快照。数据错误或模型失败 → 不产出分析。"""
    runs: list[AgentRunMeta] = []
    errors: list[str] = []

    if state.get("data_errors") or state.get("market_snapshot") is None:
        runs.append(
            AgentRunMeta(
                agent="market_agent",
                task="market_analysis",
                status="skipped_data_error",
                note="数据校验未通过,跳过分析",
            )
        )
        return {"agent_runs": runs, "llm_errors": []}

    snapshot: MarketSnapshot = state["market_snapshot"]
    indicators: dict[str, IndicatorSnapshot] = state.get("indicator_summaries", {})
    summary = summarize_snapshot(snapshot, indicators)

    # 多周期即视为多周期分析;快照周期 >= 2 时提高复杂度权重
    complexity = ComplexityInput(
        multi_timeframe=len(snapshot.series) >= 2,
    )
    messages = [
        {"role": "system", "content": _load_prompt("market")},
        {"role": "user", "content": f"symbol: {snapshot.symbol}\n{summary}"},
    ]
    resp = await deps.router.run_safe(
        TaskType.MARKET_ANALYSIS,
        messages,
        cycle_id=state["cycle_id"],
        complexity=complexity,
        response_format={"type": "json_object"},
    )
    if isinstance(resp, SafeFailure):
        errors.append(f"market_agent 模型不可用: {resp.reason}")
        runs.append(
            AgentRunMeta(
                agent="market_agent",
                task="market_analysis",
                status="llm_failed",
                note=resp.reason,
            )
        )
        return {"agent_runs": runs, "llm_errors": errors}

    try:
        analysis = parse_llm_json(resp.content, MarketAnalysis)
        analysis = analysis.model_copy(update={"symbol": snapshot.symbol})
    except Exception as e:
        errors.append(f"market_agent 输出解析失败: {e}")
        runs.append(
            AgentRunMeta(
                agent="market_agent",
                task="market_analysis",
                model_alias=resp.model_alias,
                status="parse_failed",
                note=str(e),
            )
        )
        return {"agent_runs": runs, "llm_errors": errors}

    runs.append(
        AgentRunMeta(
            agent="market_agent",
            task="market_analysis",
            model_alias=resp.model_alias,
            status="ok",
        )
    )
    return {
        "market_analysis": analysis,
        "agent_runs": runs,
        "llm_errors": [],
    }


async def strategy_agent_node(state: AgentState, deps: NodeDeps) -> dict[str, Any]:
    """Strategy Agent:与 Market Agent 并行,同一份快照,独立提案。"""
    runs: list[AgentRunMeta] = []
    errors: list[str] = []

    if state.get("data_errors") or state.get("market_snapshot") is None:
        runs.append(
            AgentRunMeta(
                agent="strategy_agent",
                task="strategy_analysis",
                status="skipped_data_error",
                note="数据校验未通过,跳过提案",
            )
        )
        return {"agent_runs": runs, "llm_errors": []}

    snapshot: MarketSnapshot = state["market_snapshot"]
    indicators: dict[str, IndicatorSnapshot] = state.get("indicator_summaries", {})
    summary = summarize_snapshot(snapshot, indicators)
    candidates = ["DCA", "TREND_FOLLOWING", "NO_TRADE"]

    messages = [
        {"role": "system", "content": _load_prompt("strategy")},
        {
            "role": "user",
            "content": (
                f"symbol: {snapshot.symbol}\n候选策略: {candidates}\n{summary}"
            ),
        },
    ]
    resp = await deps.router.run_safe(
        TaskType.STRATEGY_ANALYSIS,
        messages,
        cycle_id=state["cycle_id"],
        response_format={"type": "json_object"},
    )
    if isinstance(resp, SafeFailure):
        errors.append(f"strategy_agent 模型不可用: {resp.reason}")
        runs.append(
            AgentRunMeta(
                agent="strategy_agent",
                task="strategy_analysis",
                status="llm_failed",
                note=resp.reason,
            )
        )
        return {"agent_runs": runs, "llm_errors": errors}

    try:
        proposal = parse_llm_json(resp.content, StrategyProposal)
        proposal = proposal.model_copy(update={"symbol": snapshot.symbol})
    except Exception as e:
        errors.append(f"strategy_agent 输出解析失败: {e}")
        runs.append(
            AgentRunMeta(
                agent="strategy_agent",
                task="strategy_analysis",
                model_alias=resp.model_alias,
                status="parse_failed",
                note=str(e),
            )
        )
        return {"agent_runs": runs, "llm_errors": errors}

    runs.append(
        AgentRunMeta(
            agent="strategy_agent",
            task="strategy_analysis",
            model_alias=resp.model_alias,
            status="ok",
        )
    )
    return {
        "strategy_proposal": proposal,
        "agent_runs": runs,
        "llm_errors": [],
    }


def _synthesized_hold(state: AgentState, reason: str, status: str) -> dict[str, Any]:
    """系统合成的 HOLD 决策(不经过 LLM)。"""
    now = state.get("started_at") or int(time.time() * 1000)
    decision = TradingDecision(
        decision_id=f"cycle-{state['cycle_id']}",
        symbol=state["symbol"],
        action=Action.HOLD,
        strategy=StrategyName.NO_TRADE,
        amount_usdt=None,
        confidence=0.0,
        reason=reason,
        created_at=now,
        expires_at=now + DEFAULT_HOLD_TTL_MS,
    )
    return {
        "trading_decision": decision,
        "final_status": status,
        "final_note": reason,
        "agent_runs": [
            AgentRunMeta(
                agent="decision_agent", task="decision", status="ok", note=reason
            )
        ],
        "llm_errors": [],
    }


async def decision_agent_node(state: AgentState, deps: NodeDeps) -> dict[str, Any]:
    """Decision Agent:汇总两者输出;任何缺失/冲突 → HOLD。"""
    # 数据错误:直接合成 HOLD,不调用模型
    if state.get("data_errors"):
        reason = "数据校验未通过: " + "; ".join(state["data_errors"][:5])
        return _synthesized_hold(state, reason, "hold_data_error")

    market = state.get("market_analysis")
    strategy = state.get("strategy_proposal")
    if market is None or strategy is None:
        missing = [
            name
            for name, v in (("market_analysis", market), ("strategy_proposal", strategy))
            if v is None
        ]
        reason = f"分析缺失({'、'.join(missing)}),按规则 HOLD"
        return _synthesized_hold(state, reason, "hold_llm_failed")

    now = deps.now_ms()
    snapshot: MarketSnapshot = state["market_snapshot"]
    summary = summarize_snapshot(snapshot, state.get("indicator_summaries", {}))
    messages = [
        {"role": "system", "content": _load_prompt("decision")},
        {
            "role": "user",
            "content": (
                f"cycle 标识: cycle-{state['cycle_id']}\n"
                f"symbol: {state['symbol']}\n"
                f"当前时间戳(UTC ms): {now}\n"
                f"MarketAnalysis:\n{market.model_dump_json()}\n"
                f"StrategyProposal:\n{strategy.model_dump_json()}\n"
                f"行情摘要:\n{summary}"
            ),
        },
    ]
    resp = await deps.router.run_safe(
        TaskType.DECISION,
        messages,
        cycle_id=state["cycle_id"],
        response_format={"type": "json_object"},
    )
    if isinstance(resp, SafeFailure):
        reason = f"决策模型不可用: {resp.reason}"
        out = _synthesized_hold(state, reason, "hold_llm_failed")
        out["llm_errors"] = [reason]
        return out

    try:
        decision = parse_llm_json(resp.content, TradingDecision)
    except Exception as e:
        reason = f"决策输出解析失败,按规则 HOLD: {e}"
        out = _synthesized_hold(state, reason, "hold_llm_failed")
        out["llm_errors"] = [reason]
        return out

    # 系统覆写不信任的标识字段
    decision = decision.model_copy(
        update={
            "decision_id": f"cycle-{state['cycle_id']}",
            "symbol": state["symbol"],
            "created_at": now,
        }
    )
    if decision.expires_at <= now:
        decision = decision.model_copy(
            update={"expires_at": now + DEFAULT_HOLD_TTL_MS}
        )

    final_status = (
        "decision_ready" if decision.action == Action.BUY or decision.action == Action.SELL
        else "hold_no_trade"
    )
    return {
        "trading_decision": decision,
        "final_status": final_status,
        "final_note": decision.reason,
        "agent_runs": [
            AgentRunMeta(
                agent="decision_agent",
                task="decision",
                model_alias=resp.model_alias,
                status="ok",
            )
        ],
        "llm_errors": [],
    }


async def risk_node(state: AgentState, deps: NodeDeps) -> dict[str, Any]:
    """Risk Node(阶段 4):确定性风控,对交易计划拥有最终否决权。

    - 决策缺失 → 直接 REJECT(无计划即无放行);
    - 账户快照获取失败 → REJECT(风控信息不全时不放行);
    - 通过 → final_status=risk_approved 并携带 sized 金额;
      否决 → final_status=risk_rejected,携带逐条原因。
    """
    from risk.engine import RiskEngine  # 局部导入:打破 risk->agents 循环

    decision = state.get("trading_decision")
    if decision is None:
        return {
            "final_status": "risk_rejected",
            "final_note": "无交易计划,风控否决",
            "agent_runs": [
                AgentRunMeta(
                    agent="risk_engine", task="risk", status="rejected",
                    note="无交易计划",
                )
            ],
        }

    if deps.risk_engine is None or deps.get_account is None:
        # 安全默认:没有风控引擎就没有放行
        return {
            "final_status": "risk_rejected",
            "final_note": "未配置风控引擎,否决一切订单",
            "agent_runs": [
                AgentRunMeta(
                    agent="risk_engine", task="risk", status="rejected",
                    note="未配置风控引擎",
                )
            ],
        }

    try:
        account = await deps.get_account()
    except Exception as e:
        return {
            "final_status": "risk_rejected",
            "final_note": f"账户快照获取失败,否决: {type(e).__name__}: {e}",
            "agent_runs": [
                AgentRunMeta(
                    agent="risk_engine", task="risk", status="rejected",
                    note="账户快照失败",
                )
            ],
        }

    result = deps.risk_engine.evaluate(
        decision,
        account,
        now_ms=deps.now_ms(),
        data_errors=tuple(state.get("data_errors", [])),
    )
    if result.verdict == "APPROVED":
        final_status = "risk_approved"
        note = f"风控通过: 金额 {result.sized_amount_usdt}"
        run_status = "approved"
    elif result.verdict == "NO_ORDER":
        # HOLD 不产生订单:保留 decision 阶段的 hold_* 终态,不误标为否决
        final_status = state.get("final_status") or "hold_no_trade"
        note = "风控NO_ORDER: " + "; ".join(result.reasons[:3])
        run_status = "no_order"
    else:
        final_status = "risk_rejected"
        note = f"风控REJECTED: " + "; ".join(result.reasons[:5])
        run_status = "rejected"
    return {
        "risk_result": result,
        "account_snapshot": account,
        "final_status": final_status,
        "final_note": note,
        "agent_runs": [
            AgentRunMeta(
                agent="risk_engine",
                task="risk",
                status=run_status,
                note=note,
            )
        ],
    }


async def execute_node(state: AgentState, deps: NodeDeps) -> dict[str, Any]:
    """Execute Node(阶段 5):风控放行后经 Broker 接口提交订单。

    安全规则(全部 fail-safe):
    - 无风控放行(risk_result 缺失 / 未批准 / 无订单)→ exec_rejected,不下单;
    - 未配置 order_manager → exec_rejected(没有执行层就没有成交);
    - Broker 返回 REJECTED → exec_rejected(记录原因,不注册已执行);
    - Broker 返回 NEW / PARTIALLY_FILLED / FILLED → executed,
      并向风控注册 decision_id(防同 decision 重复下单);
    - 提交异常 / UNKNOWN → exec_unknown(结果未知,
      同样注册防重复;恢复走 OrderManager.recover,绝不自动重发)。
    """
    decision = state.get("trading_decision")
    risk_result = state.get("risk_result")

    def _reject(note: str, run_status: str = "rejected") -> dict[str, Any]:
        return {
            "final_status": "exec_rejected",
            "final_note": f"执行层拒绝: {note}",
            "agent_runs": [
                AgentRunMeta(
                    agent="execution", task="execute", status=run_status, note=note
                )
            ],
        }

    if risk_result is None or not risk_result.approved or risk_result.order is None:
        # 无风控放行不允许执行(即使误挂了 execute_node)
        return _reject("无风控放行,拒绝执行")
    if deps.order_manager is None:
        return _reject("未配置执行层(Broker),拒绝执行")
    if decision is None:
        return _reject("交易决策缺失,拒绝执行")

    order = risk_result.order
    try:
        result = await deps.order_manager.submit(order)
    except Exception as e:
        # Manager 内部应把异常转成 UNKNOWN;此处兜底
        return {
            "final_status": "exec_unknown",
            "final_note": f"执行层提交异常: {type(e).__name__}: {e}",
            "agent_runs": [
                AgentRunMeta(
                    agent="execution", task="execute", status="unknown",
                    note=f"{type(e).__name__}: {e}",
                )
            ],
        }

    # 已提交(含 UNKNOWN:订单可能存在)→ 注册已执行,防同 decision 重复下单;
    # REJECTED(明确未成交)不注册,允许修正后重新决策
    if deps.risk_engine is not None and result.status.value != "REJECTED":
        deps.risk_engine.register_executed(decision.decision_id)

    if result.status.value in {"NEW", "PARTIALLY_FILLED", "FILLED"}:
        final_status = "executed"
        note = (
            f"订单{result.status.value}: {result.side} {result.filled_qty}/"
            f"{result.requested_qty} @ {result.avg_price}"
        )
        run_status = "executed"
    elif result.status.value == "UNKNOWN":
        final_status = "exec_unknown"
        note = f"订单状态 UNKNOWN(结果未知,待对账): {result.error}"
        run_status = "unknown"
    else:  # REJECTED / CANCELED / EXPIRED 等非成交终态
        final_status = "exec_rejected"
        note = f"订单{result.status.value}: {result.error or '无成交'}"
        run_status = result.status.value.lower()

    return {
        "order_result": result,
        "final_status": final_status,
        "final_note": note,
        "agent_runs": [
            AgentRunMeta(
                agent="execution", task="execute", status=run_status, note=note
            )
        ],
    }


def record_node(state: AgentState, deps: NodeDeps) -> dict[str, Any]:
    """记录本轮审计(JSONL 主备份;阶段 7 可选入库,失败降级不影响 cycle)。"""
    decision = state.get("trading_decision")
    risk_result = state.get("risk_result")
    order_result = state.get("order_result")
    entry = {
        "cycle_id": state.get("cycle_id"),
        "symbol": state.get("symbol"),
        "final_status": state.get("final_status"),
        "decision": decision.model_dump(mode="json") if decision else None,
        "risk": risk_result.to_audit_dict() if risk_result is not None else None,
        "order": order_result.model_dump(mode="json") if order_result else None,
        "market_sentiment": (
            state["market_analysis"].sentiment.value
            if state.get("market_analysis")
            else None
        ),
        "strategy": (
            state["strategy_proposal"].strategy.value
            if state.get("strategy_proposal")
            else None
        ),
        "llm_errors": state.get("llm_errors", []),
        "data_errors": state.get("data_errors", []),
    }
    if deps.audit_path is not None:
        deps.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with deps.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # 阶段 7:入库(旁路;DB 故障绝不打断 cycle,JSONL 是主备份)
    if deps.recorder is not None:
        try:
            usage = deps.router.usage.records_for_cycle(state.get("cycle_id", ""))
            deps.recorder.record_cycle(state, usage_records=usage)
        except Exception as e:
            try:
                deps.recorder.record_system_event(
                    "db_write_failed",
                    f"cycle 审计入库失败(已降级为 JSONL): {type(e).__name__}: {e}",
                    cycle_id=state.get("cycle_id"),
                    severity="WARNING",
                )
            except Exception:
                pass  # 数据库整体不可用:放弃入库,交易主流程不受影响

    # 阶段 7:指标
    if deps.metrics is not None:
        final_status = state.get("final_status", "unknown")
        deps.metrics.record_cycle(final_status)
        for run in state.get("agent_runs", []):
            status = getattr(run, "status", None)
            if status and ("failed" in status or status == "rejected"):
                deps.metrics.record_agent_failure(getattr(run, "agent", "?"))
        if risk_result is not None:
            deps.metrics.record_risk_verdict(getattr(risk_result, "verdict", "?"))
        if order_result is not None:
            deps.metrics.record_order_status(
                getattr(order_result, "status", "?"),
                side=getattr(order_result, "side", None),
            )
            if getattr(order_result, "filled_qty", 0):
                deps.metrics.record_trade(
                    side=getattr(order_result, "side", "?") or "?",
                    symbol=getattr(order_result, "symbol", None) or state.get("symbol", "?"),
                )
        account = state.get("account_snapshot")
        if account is not None:
            deps.metrics.record_account(
                float(getattr(account, "equity_usdt", 0.0) or 0.0),
                float(getattr(account, "available_usdt", 0.0) or 0.0),
            )

    # 阶段 7:Telegram 通知(旁路,仅重要终态;绝不执行任何聊天指令)
    if deps.notifier is not None and deps.notifier.enabled:
        final_status = state.get("final_status", "")
        if final_status in (
            "executed", "exec_rejected", "exec_unknown", "risk_rejected",
        ):
            deps.notifier.send_cycle_summary(
                cycle_id=state.get("cycle_id", "?"),
                symbol=state.get("symbol", "?"),
                final_status=final_status,
                decision_id=getattr(decision, "decision_id", None),
                action=getattr(decision, "action", None),
                amount_usdt=getattr(risk_result, "sized_amount_usdt", None)
                if risk_result is not None else None,
                risk_verdict=getattr(risk_result, "verdict", None)
                if risk_result is not None else None,
                note=state.get("final_note"),
            )
    return {}
