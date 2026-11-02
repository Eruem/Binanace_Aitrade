"""阶段 1:ModelRouter 测试(指导书 14.1-14.4)。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from llm.base import LLMFatalError, LLMProvider, LLMRetryableError
from llm.complexity import ComplexityInput, ComplexityScorer
from llm.fallback import FallbackPolicy
from llm.registry import ModelRegistry
from llm.router import ModelRouter
from llm.schemas import LLMResponse, SafeAction, TaskType
from llm.usage_tracker import UsageTracker


@dataclass
class FakeSpec:
    provider: str
    model: str
    temperature: float = 0.1
    max_tokens: int = 100
    timeout: int = 10
    supports_json: bool = True


@dataclass
class ScriptedCall:
    """一次脚本化调用结果:成功内容或要抛的异常。"""

    content: str | None = None
    error: Exception | None = None


class ScriptedProvider(LLMProvider):
    """按调用顺序吐出脚本结果的 Provider。"""

    def __init__(self, name: str, script: list[ScriptedCall]) -> None:
        self.name = name
        self.script = list(script)
        self.calls = 0

    async def generate(self, **kwargs: Any) -> LLMResponse:
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        step = self.script[idx]
        if step.error is not None:
            raise step.error
        return LLMResponse(
            content=step.content or "",
            provider=self.name,
            model=kwargs["model"],
            model_alias="",
            latency_ms=1.0,
        )


def _make_router(
    primary_script: list[ScriptedCall],
    backup_script: list[ScriptedCall] | None = None,
    *,
    max_calls: int = 8,
    fallback_enabled: bool = True,
) -> tuple[ModelRouter, ScriptedProvider, ScriptedProvider | None]:
    primary = ScriptedProvider("primary", primary_script)
    backup = (
        ScriptedProvider("secondary", backup_script)
        if backup_script is not None
        else None
    )
    providers = {"primary": primary}
    if backup is not None:
        providers["secondary"] = backup

    models = {
        "fast": FakeSpec(provider="primary", model="fast-model"),
        "reasoning": FakeSpec(provider="primary", model="reason-model"),
    }
    if backup is not None:
        models["backup"] = FakeSpec(provider="secondary", model="backup-model")
    registry = ModelRegistry(
        providers_config=providers,  # type: ignore[arg-type]
        models_config=models,  # type: ignore[arg-type]
        provider_factory=lambda name, cfg: cfg,  # 已构造好的实例直接用
    )
    router = ModelRouter(
        registry=registry,
        routing={
            "market_summary": "fast",
            "market_analysis": "fast",
            "strategy_analysis": "reasoning",
            "decision": "reasoning",
            "anomaly_analysis": "reasoning",
            "risk_explanation": "fast",
        },
        fallback=FallbackPolicy(enabled=fallback_enabled, model_alias="backup", max_attempts=2),
        limits_max_calls_per_cycle=max_calls,
        max_concurrent_requests=3,
        complexity_scorer=ComplexityScorer(threshold=3),
        usage_tracker=UsageTracker(),
    )
    return router, primary, backup


class TestRouting:
    async def test_task_selects_expected_alias(self):
        router, _, _ = _make_router([ScriptedCall(content="ok")])
        assert router.select_alias(TaskType.MARKET_SUMMARY) == "fast"
        assert router.select_alias(TaskType.RISK_EXPLANATION) == "fast"
        assert router.select_alias(TaskType.STRATEGY_ANALYSIS) == "reasoning"
        assert router.select_alias(TaskType.DECISION) == "reasoning"
        assert router.select_alias(TaskType.ANOMALY_ANALYSIS) == "reasoning"

    async def test_market_analysis_low_complexity_stays_fast(self):
        router, _, _ = _make_router([ScriptedCall(content="ok")])
        alias = router.select_alias(
            TaskType.MARKET_ANALYSIS, ComplexityInput(multi_timeframe=False)
        )
        assert alias == "fast"

    async def test_market_analysis_high_complexity_upgrades(self):
        router, _, _ = _make_router([ScriptedCall(content="ok")])
        alias = router.select_alias(
            TaskType.MARKET_ANALYSIS, ComplexityInput(agent_conflict=True)
        )
        assert alias == "reasoning"

    async def test_uses_reasoning_model_on_decision(self):
        router, primary, _ = _make_router([ScriptedCall(content="ok")])
        resp = await router.run(
            TaskType.DECISION, [{"role": "user", "content": "x"}], cycle_id="c1"
        )
        assert resp.model == "reason-model"
        assert primary.calls == 1


class TestRetryAndFallback:
    async def test_retryable_then_success(self):
        script = [
            ScriptedCall(error=LLMRetryableError("timeout")),
            ScriptedCall(content="ok"),
        ]
        router, primary, _ = _make_router(script)
        resp = await router.run(
            TaskType.MARKET_SUMMARY, [{"role": "user", "content": "x"}], cycle_id="c1"
        )
        assert resp.content == "ok"
        assert primary.calls == 2

    async def test_primary_exhausted_switches_to_backup(self):
        script = [
            ScriptedCall(error=LLMRetryableError("t1")),
            ScriptedCall(error=LLMRetryableError("t2")),
        ]
        router, primary, backup = _make_router(
            script, [ScriptedCall(content="backup-ok")]
        )
        resp = await router.run(
            TaskType.MARKET_SUMMARY, [{"role": "user", "content": "x"}], cycle_id="c1"
        )
        assert resp.content == "backup-ok"
        assert primary.calls == 2
        assert backup is not None and backup.calls == 1

    async def test_fatal_switches_immediately(self):
        router, primary, backup = _make_router(
            [ScriptedCall(error=LLMFatalError("auth"))],
            [ScriptedCall(content="backup-ok")],
        )
        resp = await router.run(
            TaskType.MARKET_SUMMARY, [{"role": "user", "content": "x"}], cycle_id="c1"
        )
        assert resp.content == "backup-ok"
        assert primary.calls == 1  # fatal 不原地重试

    async def test_all_models_fail_raises(self):
        from llm.base import AllModelsUnavailableError

        router, _, _ = _make_router(
            [ScriptedCall(error=LLMRetryableError("t"))],
            [ScriptedCall(error=LLMFatalError("dead"))],
        )
        with pytest.raises(AllModelsUnavailableError):
            await router.run(
                TaskType.MARKET_SUMMARY,
                [{"role": "user", "content": "x"}],
                cycle_id="c1",
            )

    async def test_run_safe_returns_hold_failure(self):
        router, _, _ = _make_router(
            [ScriptedCall(error=LLMRetryableError("t"))],
            [ScriptedCall(error=LLMFatalError("dead"))],
        )
        out = await router.run_safe(
            TaskType.MARKET_SUMMARY,
            [{"role": "user", "content": "x"}],
            cycle_id="c1",
        )
        assert out.action == SafeAction.HOLD
        assert out.task == "market_summary"


class TestLimits:
    async def test_cycle_limit_blocks_extra_calls(self):
        from llm.base import CycleLimitExceededError

        # max_calls=2:两次成功调用后第三次必须被拒绝
        router, _, _ = _make_router([ScriptedCall(content="ok")], max_calls=2)
        await router.run(TaskType.MARKET_SUMMARY, [], cycle_id="c1")
        await router.run(TaskType.MARKET_SUMMARY, [], cycle_id="c1")
        with pytest.raises(CycleLimitExceededError):
            await router.run(TaskType.MARKET_SUMMARY, [], cycle_id="c1")

    async def test_limit_is_per_cycle(self):
        router, _, _ = _make_router([ScriptedCall(content="ok")], max_calls=1)
        await router.run(TaskType.MARKET_SUMMARY, [], cycle_id="c1")
        # 新 cycle 不共享限额
        resp = await router.run(TaskType.MARKET_SUMMARY, [], cycle_id="c2")
        assert resp.content == "ok"

    async def test_unknown_task_rejected(self):
        router, _, _ = _make_router([ScriptedCall(content="ok")])
        with pytest.raises(Exception):
            router.select_alias("not_a_task")


class TestUsage:
    async def test_usage_recorded(self):
        router, _, _ = _make_router([ScriptedCall(content="ok")])
        await router.run(TaskType.DECISION, [], cycle_id="c1")
        summary = router.usage.summary("c1")
        assert summary["total_calls"] == 1
        assert summary["ok_calls"] == 1
        recs = router.usage.records_for_cycle("c1")
        assert recs[0].model_alias == "reasoning"
        assert recs[0].task == "decision"

    async def test_errors_recorded(self):
        router, _, _ = _make_router(
            [ScriptedCall(error=LLMRetryableError("t"))],
            [ScriptedCall(error=LLMFatalError("d"))],
        )
        await router.run_safe(TaskType.MARKET_SUMMARY, [], cycle_id="c1")
        recs = router.usage.records_for_cycle("c1")
        assert any(r.status == "error" for r in recs)

    def test_from_configset_wires_usage_sink(self, tmp_path):
        """from_configset 把 settings.paths.usage_log 接到用量落盘。

        回归背景:UsageTracker 支持 sink_path,但 from_configset 创建时
        从未传入,配置里的 usage_log 形同虚设,token 用量无从统计。
        """
        from config import load_config
        from llm.usage_tracker import UsageRecord

        cfg = load_config()
        sink = tmp_path / "usage.jsonl"
        cfg.settings.paths.usage_log = str(sink)
        router = ModelRouter.from_configset(cfg)
        router.usage.record(
            UsageRecord(
                cycle_id="c1",
                task="market_analysis",
                model_alias="fast",
                provider="primary",
                model="m",
                latency_ms=1.0,
                input_tokens=10,
                output_tokens=5,
                status="ok",
            )
        )
        assert sink.exists()
        assert "market_analysis" in sink.read_text(encoding="utf-8")
