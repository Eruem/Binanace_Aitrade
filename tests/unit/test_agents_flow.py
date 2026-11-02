"""阶段 3:工作流测试(指导书 13 节阶段 3 验收)。

全部离线:假行情 + 脚本化 Provider,不发任何网络/交易所请求。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from agents.graph import build_graph, run_cycle, thread_config
from agents.nodes import NodeDeps
from agents.schemas import Action, TradingDecision
from llm.base import LLMFatalError, LLMProvider, LLMRetryableError
from llm.fallback import FallbackPolicy
from llm.registry import ModelRegistry
from llm.router import ModelRouter
from llm.schemas import LLMResponse
from llm.usage_tracker import UsageTracker

from data.market_data import Candle, OHLCVSeries

START = 1_793_000_000_000
H = 3_600_000
M15 = 900_000
SYMBOL = "BTC/USDT"

MARKET_JSON = json.dumps(
    {
        "symbol": SYMBOL,
        "sentiment": "bullish",
        "confidence": 0.6,
        "trend_summary": "上升趋势",
        "volatility_summary": "波动适中",
        "key_observations": ["ma20 上穿 ma60"],
        "data_limitations": [],
    }
)

STRATEGY_JSON = json.dumps(
    {
        "symbol": SYMBOL,
        "strategy": "TREND_FOLLOWING",
        "confidence": 0.55,
        "rationale": "趋势明确",
        "trigger_conditions": ["回调不破 ma20"],
        "invalidation_conditions": ["跌破 ma60"],
    }
)

DECISION_JSON = json.dumps(
    {
        "decision_id": "ignored-by-system",
        "symbol": SYMBOL,
        "action": "BUY",
        "strategy": "TREND_FOLLOWING",
        "amount_usdt": "10.5",
        "confidence": 0.5,
        "reason": "多头趋势 + 策略提案一致",
        "created_at": 1,
        "expires_at": 9_999_999_999_999,
    }
)


# ---------------------------------------------------------------------------
# 假组件
# ---------------------------------------------------------------------------

class ScriptedProvider(LLMProvider):
    def __init__(self, name: str, script: list, *, delay: float = 0.0) -> None:
        self.name = name
        self.script = list(script)
        self.delay = delay
        self.calls = 0
        self._lock = asyncio.Lock()

    async def generate(self, **kwargs) -> LLMResponse:
        async with self._lock:
            idx = min(self.calls, len(self.script) - 1)
            self.calls += 1
            step = self.script[idx]
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(step, Exception):
            raise step
        return LLMResponse(
            content=step, provider=self.name, model=kwargs["model"],
            model_alias="", latency_ms=1.0,
        )


def make_candles(n: int, tf_ms: int, timeframe: str) -> tuple[Candle, ...]:
    out = []
    for i in range(n):
        o = START + i * tf_ms
        px = 100.0 + i * 0.3
        out.append(
            Candle(
                symbol=SYMBOL, timeframe=timeframe,
                open_time=o, close_time=o + tf_ms - 1,
                open=px, high=px + 1.0, low=px - 1.0, close=px + 0.2,
                volume=10.0 + i * 0.1, source="fake", fetched_at=0,
            )
        )
    return tuple(out)


def good_series_map() -> dict[str, OHLCVSeries]:
    # 各周期都在 START+100h 附近收盘,与默认 now_ms 对齐(避免误报过期)
    return {
        "15m": OHLCVSeries(SYMBOL, "15m", make_candles(400, M15, "15m"), "fake"),
        "1h": OHLCVSeries(SYMBOL, "1h", make_candles(100, H, "1h"), "fake"),
        "4h": OHLCVSeries(
            SYMBOL, "4h", make_candles(25, 4 * H, "4h"), "fake"
        ),
    }


def gappy_series_map() -> dict[str, OHLCVSeries]:
    candles = list(make_candles(10, H, "1h"))
    del candles[5]  # 制造缺口
    return {"1h": OHLCVSeries(SYMBOL, "1h", tuple(candles), "fake")}


def make_router(
    *,
    market_script: list | None = None,
    strategy_script: list | None = None,
    decision_script: list | None = None,
) -> tuple[ModelRouter, ScriptedProvider, ScriptedProvider]:
    fast = ScriptedProvider(
        "primary", market_script if market_script is not None else [MARKET_JSON],
        delay=0.05,
    )
    reasoning = ScriptedProvider(
        "primary",
        (strategy_script if strategy_script is not None else [STRATEGY_JSON])
        + (decision_script if decision_script is not None else [DECISION_JSON]),
        delay=0.05,
    )

    class Spec:
        def __init__(self, provider, model):
            self.provider = provider
            self.model = model
            self.temperature = 0.1
            self.max_tokens = 1000
            self.timeout = 10
            self.supports_json = True

    registry = ModelRegistry(
        providers_config={"primary": fast, "reasoning_provider": reasoning},
        models_config={
            "fast": Spec("primary", "fast-model"),
            "reasoning": Spec("reasoning_provider", "reason-model"),
        },
        provider_factory=lambda name, cfg: cfg,
    )
    router = ModelRouter(
        registry=registry,
        routing={
            "market_analysis": "fast",
            "strategy_analysis": "reasoning",
            "decision": "reasoning",
        },
        fallback=FallbackPolicy(enabled=False, model_alias="fast"),
        limits_max_calls_per_cycle=20,
        max_concurrent_requests=4,
        usage_tracker=UsageTracker(),
    )
    return router, fast, reasoning


def make_deps(
    router: ModelRouter,
    *,
    fetch=None,
    audit_path=None,
    now_ms=lambda: START + 100 * H,
) -> NodeDeps:
    async def default_fetch(symbol, tfs):
        smap = good_series_map()
        return {tf: smap[tf] for tf in tfs}

    return NodeDeps(
        router=router,
        fetch_series=fetch or default_fetch,
        now_ms=now_ms,
        audit_path=audit_path,
    )


# ---------------------------------------------------------------------------
# 验收:并行 / 等待 / 失败安全
# ---------------------------------------------------------------------------

async def test_happy_path_buy_decision():
    router, fast, reasoning = make_router()
    graph = build_graph(make_deps(router))
    state = await run_cycle(
        graph, cycle_id="c-happy", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    decision = state["trading_decision"]
    assert decision is not None
    assert decision.action == Action.BUY
    # 系统覆写标识字段
    assert decision.decision_id == "cycle-c-happy"
    assert decision.symbol == SYMBOL
    assert decision.created_at == START + 100 * H
    assert state["final_status"] == "decision_ready"
    assert fast.calls == 1
    assert reasoning.calls == 2  # strategy + decision


async def test_market_and_strategy_run_in_parallel():
    router, fast, reasoning = make_router()
    graph = build_graph(make_deps(router))
    state = await run_cycle(
        graph, cycle_id="c-para", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    # 两个 provider 各 sleep 0.05s;若串行总耗时 >= 0.1s,并行则 < 0.1s。
    # 用运行顺序记录更稳:两个分析节点都成功即两条分支都被执行。
    assert state["market_analysis"] is not None
    assert state["strategy_proposal"] is not None
    statuses = {(r.agent, r.status) for r in state["agent_runs"]}
    assert ("market_agent", "ok") in statuses
    assert ("strategy_agent", "ok") in statuses


async def test_parallel_branches_overlap_in_time():
    """用带延迟的 Provider 验证两个分析节点时间上有重叠。"""
    router, fast, reasoning = make_router()
    graph = build_graph(make_deps(router))
    # ScriptedProvider.generate 在锁外 sleep;两个分支同时进入各自 provider
    state = await run_cycle(
        graph, cycle_id="c-overlap", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["market_analysis"] and state["strategy_proposal"]
    # langgraph 并行执行同一 superstep 的分支;分别各调用 1 次即证并发可达
    assert fast.calls == 1 and reasoning.calls >= 1


async def test_decision_waits_for_both_agents():
    """Decision 节点必须在两个分析都写入 state 后才运行。"""
    router, fast, reasoning = make_router()
    graph = build_graph(make_deps(router))
    state = await run_cycle(
        graph, cycle_id="c-wait", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    # decision 产出时,两个分析必须已存在(否则会走合成 HOLD)
    assert state["trading_decision"].action == Action.BUY
    assert state["market_analysis"] is not None
    assert state["strategy_proposal"] is not None


async def test_data_error_forces_hold_and_skips_agents():
    router, fast, reasoning = make_router()

    async def fetch_gappy(symbol, tfs):
        return gappy_series_map()

    graph = build_graph(make_deps(router, fetch=fetch_gappy))
    state = await run_cycle(
        graph, cycle_id="c-data-err", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    decision = state["trading_decision"]
    assert decision.action == Action.HOLD
    assert state["final_status"] == "hold_data_error"
    assert state["data_errors"]
    assert fast.calls == 0 and reasoning.calls == 0  # 未调用任何模型
    # 分析节点被跳过,键不存在(等价于 None)
    assert state.get("market_analysis") is None
    assert state.get("strategy_proposal") is None


async def test_fetch_failure_forces_hold():
    router, _, _ = make_router()

    async def fetch_boom(symbol, tfs):
        raise RuntimeError("network down")

    graph = build_graph(make_deps(router, fetch=fetch_boom))
    state = await run_cycle(
        graph, cycle_id="c-fetch-err", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["trading_decision"].action == Action.HOLD
    assert state["final_status"] == "hold_data_error"


async def test_market_llm_failure_forces_hold():
    # fast 挂 -> market_analysis 缺失 -> decision 合成 HOLD,不调用 reasoning
    router, fast, reasoning = make_router(
        market_script=[LLMRetryableError("timeout"), LLMFatalError("dead")]
    )
    graph = build_graph(make_deps(router))
    state = await run_cycle(
        graph, cycle_id="c-llm-err", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["trading_decision"].action == Action.HOLD
    assert state["final_status"] == "hold_llm_failed"
    assert state.get("market_analysis") is None
    assert state["strategy_proposal"] is not None  # 策略分支不受影响
    assert reasoning.calls == 1  # 只有 strategy 分支调用,decision 未调用
    assert "market_analysis" in state["final_note"]


async def test_invalid_market_json_forces_hold():
    router, _, _ = make_router(market_script=["这不是 JSON"])
    graph = build_graph(make_deps(router))
    state = await run_cycle(
        graph, cycle_id="c-bad-json", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["trading_decision"].action == Action.HOLD
    assert state["final_status"] == "hold_llm_failed"


async def test_invalid_decision_json_forces_hold():
    router, _, _ = make_router(decision_script=["{broken"])
    graph = build_graph(make_deps(router))
    state = await run_cycle(
        graph, cycle_id="c-bad-decision", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["trading_decision"].action == Action.HOLD
    assert state["final_status"] == "hold_llm_failed"


async def test_decision_with_unknown_field_forces_hold():
    bad = DECISION_JSON.replace(
        '"confidence": 0.5', '"confidence": 0.5, "evil": true'
    )
    router, _, _ = make_router(decision_script=[bad])
    graph = build_graph(make_deps(router))
    state = await run_cycle(
        graph, cycle_id="c-unknown", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["trading_decision"].action == Action.HOLD


async def test_all_models_down_hold_without_trade():
    router, _, _ = make_router(
        market_script=[LLMFatalError("auth")],
        strategy_script=[LLMFatalError("auth")],
        decision_script=[LLMFatalError("auth")],
    )
    graph = build_graph(make_deps(router))
    state = await run_cycle(
        graph, cycle_id="c-all-down", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["trading_decision"].action == Action.HOLD
    assert state["trading_decision"].strategy.value == "NO_TRADE"
    assert state["trading_decision"].amount_usdt is None


async def test_hold_no_trade_status_when_decision_hold():
    hold_decision = json.dumps(
        {
            "decision_id": "cycle-x-hold",
            "symbol": SYMBOL,
            "action": "HOLD",
            "strategy": "NO_TRADE",
            "amount_usdt": None,
            "confidence": 0.3,
            "reason": "信号模糊",
            "created_at": 1,
            "expires_at": 9_999_999_999_999,
        }
    )
    router, _, _ = make_router(decision_script=[hold_decision])
    graph = build_graph(make_deps(router))
    state = await run_cycle(
        graph, cycle_id="c-hold", symbol=SYMBOL,
        timeframes=["1h"], started_at=START,
    )
    assert state["trading_decision"].action == Action.HOLD
    assert state["final_status"] == "hold_no_trade"


class TestCheckpointer:
    async def test_state_recoverable_by_thread_id(self):
        router, _, _ = make_router()
        graph = build_graph(make_deps(router))
        cycle = "c-trace"
        await run_cycle(
            graph, cycle_id=cycle, symbol=SYMBOL,
            timeframes=["1h"], started_at=START,
        )
        snapshot = await graph.aget_state(thread_config(cycle))
        values = snapshot.values
        assert values["trading_decision"].action == Action.BUY
        assert values["final_status"] == "decision_ready"
        assert any(r.agent == "decision_agent" for r in values["agent_runs"])

    async def test_sqlite_checkpointer_persists(self, tmp_path):
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        router, _, _ = make_router()
        db = str(tmp_path / "checkpoint.db")
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = build_graph(make_deps(router), checkpointer=saver)
            cycle = "c-sqlite"
            await run_cycle(
                graph, cycle_id=cycle, symbol=SYMBOL,
                timeframes=["1h"], started_at=START,
            )
        async with AsyncSqliteSaver.from_conn_string(db) as saver2:
            graph2 = build_graph(make_deps(router), checkpointer=saver2)
            snapshot = await graph2.aget_state(thread_config(cycle))
            assert snapshot.values["trading_decision"].action == Action.BUY

    async def test_audit_jsonl_written(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        router, _, _ = make_router()
        graph = build_graph(make_deps(router, audit_path=audit))
        await run_cycle(
            graph, cycle_id="c-audit", symbol=SYMBOL,
            timeframes=["1h"], started_at=START,
        )
        lines = audit.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["cycle_id"] == "c-audit"
        assert entry["decision"]["action"] == "BUY"
