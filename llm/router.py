"""Dynamic Model Router(指导书 5.3/5.4)。

规则路由 + 复杂度评分 + 备用模型。不使用额外 LLM 决定调用哪个模型。

路由规则:
- market_summary / risk_explanation      -> fast
- strategy_analysis / decision / anomaly -> reasoning
- market_analysis                        -> fast;复杂度 >= 阈值时升级 reasoning

失败与限额:
- retryable 错误在同一模型上重试,最多 max_attempts 次;
- fatal 错误或重试耗尽 -> 切换备用模型;
- 全部失败 -> 抛 AllModelsUnavailableError,并提供 safe_failure() 供
  工作流映射为 HOLD / NO_TRADE(绝不生成交易指令);
- 每个 cycle 的调用总数受 max_calls_per_cycle 限制;
- 并发受 max_concurrent_requests 信号量限制。
"""

from __future__ import annotations

import asyncio
from typing import Any

from .base import (
    AllModelsUnavailableError,
    CycleLimitExceededError,
    LLMError,
    LLMRetryableError,
    LLMResponseParseError,
)
from .complexity import ComplexityInput, ComplexityScorer
from .fallback import FallbackPolicy
from .registry import ModelRegistry
from .schemas import LLMResponse, SafeAction, SafeFailure, TaskType
from .usage_tracker import UsageRecord, UsageTracker


class ModelRouter:
    """Agent 访问模型的唯一入口。"""

    def __init__(
        self,
        *,
        registry: ModelRegistry,
        routing: dict[str, str],
        fallback: FallbackPolicy,
        limits_max_calls_per_cycle: int = 8,
        max_concurrent_requests: int = 3,
        complexity_scorer: ComplexityScorer | None = None,
        usage_tracker: UsageTracker | None = None,
    ) -> None:
        self._registry = registry
        self._routing = dict(routing)
        self._fallback = fallback
        self._max_calls = limits_max_calls_per_cycle
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._scorer = complexity_scorer or ComplexityScorer()
        self._usage = usage_tracker or UsageTracker()

    @classmethod
    def from_configset(
        cls, configset: Any, *, registry: ModelRegistry | None = None
    ) -> "ModelRouter":
        mf = configset.models_file
        reg = registry or ModelRegistry.from_configset(configset)
        # 用量落盘:settings.paths.usage_log;注入的配置缺该字段时
        # getattr 返回 None,保持仅内存(与旧行为一致)
        sink = getattr(
            getattr(getattr(configset, "settings", None), "paths", None),
            "usage_log",
            None,
        )
        return cls(
            registry=reg,
            routing=mf.routing,
            fallback=FallbackPolicy.from_config(mf.fallback),
            limits_max_calls_per_cycle=mf.limits.max_calls_per_cycle,
            max_concurrent_requests=mf.limits.max_concurrent_requests,
            complexity_scorer=ComplexityScorer(threshold=mf.limits.complexity_threshold),
            usage_tracker=UsageTracker(sink_path=sink),
        )

    # ------------------------------------------------------------------
    # 路由选择
    # ------------------------------------------------------------------
    def select_alias(
        self, task: TaskType | str, complexity: ComplexityInput | None = None
    ) -> str:
        task_key = task.value if isinstance(task, TaskType) else str(task)
        if task_key not in self._routing:
            raise LLMResponseParseError(f"未配置路由的任务类型: {task_key!r}")
        alias = self._routing[task_key]
        if (
            task_key == TaskType.MARKET_ANALYSIS.value
            and complexity is not None
            and self._scorer.should_upgrade(complexity)
        ):
            return "reasoning" if "reasoning" in self._registry.aliases() else alias
        return alias

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def run(
        self,
        task: TaskType | str,
        messages: list[dict[str, Any]],
        *,
        cycle_id: str,
        complexity: ComplexityInput | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """执行一次带路由、重试、备用与限额的模型调用。

        Raises:
            CycleLimitExceededError: 超过每轮调用上限。
            AllModelsUnavailableError: 所有候选模型均失败。
        """
        task_key = task.value if isinstance(task, TaskType) else str(task)
        if self._usage.calls_in_cycle(cycle_id) >= self._max_calls:
            raise CycleLimitExceededError(
                f"cycle {cycle_id} 已达 max_calls_per_cycle={self._max_calls}"
            )

        primary = self.select_alias(task_key, complexity)
        # 只保留已注册的候选别名;真实配置经 ConfigSet 校验过,这里兜底
        # 直接构造 Registry 的场景(如测试或代码内装配)。
        chain = [
            a for a in self._fallback.candidate_chain(primary)
            if a in self._registry.aliases()
        ]
        if not chain:
            raise AllModelsUnavailableError(
                f"任务 {task_key!r} 的模型别名 {primary!r} 未注册,转 HOLD / NO_TRADE"
            )
        attempts_made = 0
        last_error: LLMError | None = None

        for alias in chain:
            spec = self._registry.spec(alias)
            if response_format is not None and not spec.supports_json:
                # 不支持 JSON 模式的模型不带 response_format 调用;
                # 解析层仍会用业务 Schema 严格校验。
                rf = None
            else:
                rf = response_format

            fatal_for_model = False
            for _ in range(self._fallback.max_attempts):
                if self._usage.calls_in_cycle(cycle_id) >= self._max_calls:
                    raise CycleLimitExceededError(
                        f"cycle {cycle_id} 已达 max_calls_per_cycle={self._max_calls}"
                    )
                attempts_made += 1
                try:
                    resp = await self._call(
                        alias=alias,
                        task_key=task_key,
                        messages=messages,
                        response_format=rf,
                        cycle_id=cycle_id,
                    )
                    return resp
                except LLMError as e:
                    last_error = e
                    self._record_error(cycle_id, task_key, alias, e)
                    if not e.retryable:
                        fatal_for_model = True
                        break
                    # retryable:继续在本模型上重试
            if fatal_for_model:
                # 鉴权/参数类错误:换备用模型而不是原地重试
                continue

        raise AllModelsUnavailableError(
            f"任务 {task_key!r} 的全部模型均不可用"
            f"(尝试 {attempts_made} 次);工作流必须转为 HOLD / NO_TRADE"
        )

    async def run_safe(
        self,
        task: TaskType | str,
        messages: list[dict[str, Any]],
        *,
        cycle_id: str,
        complexity: ComplexityInput | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse | SafeFailure:
        """run() 的安全包装:模型全挂时返回 SafeFailure(HOLD)而不抛异常。"""
        try:
            return await self.run(
                task,
                messages,
                cycle_id=cycle_id,
                complexity=complexity,
                response_format=response_format,
            )
        except (AllModelsUnavailableError, CycleLimitExceededError) as e:
            return self.safe_failure(
                task=str(task.value if isinstance(task, TaskType) else task),
                reason=str(e),
                attempts=self._usage.calls_in_cycle(cycle_id),
            )

    @staticmethod
    def safe_failure(*, task: str, reason: str, attempts: int = 0) -> SafeFailure:
        """全部模型不可用时的唯一安全输出。"""
        return SafeFailure(
            task=task,
            action=SafeAction.HOLD,
            reason=reason,
            attempts=attempts,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    async def _call(
        self,
        *,
        alias: str,
        task_key: str,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None,
        cycle_id: str,
    ) -> LLMResponse:
        import time

        spec = self._registry.spec(alias)
        provider = self._registry.provider_for(alias)
        started = time.perf_counter()
        try:
            async with self._semaphore:
                resp = await provider.generate(
                    model=spec.model,
                    messages=messages,
                    temperature=spec.temperature,
                    max_tokens=spec.max_tokens,
                    timeout=float(spec.timeout),
                    response_format=response_format,
                )
        except LLMError:
            raise
        latency_ms = resp.latency_ms or (time.perf_counter() - started) * 1000.0
        stamped = resp.model_copy(update={"model_alias": alias, "latency_ms": latency_ms})
        self._usage.record(
            UsageRecord(
                cycle_id=cycle_id,
                task=task_key,
                model_alias=alias,
                provider=stamped.provider,
                model=stamped.model,
                latency_ms=latency_ms,
                input_tokens=stamped.input_tokens,
                output_tokens=stamped.output_tokens,
                status="ok",
                request_id=stamped.request_id,
            )
        )
        return stamped

    def _record_error(
        self, cycle_id: str, task_key: str, alias: str, err: LLMError
    ) -> None:
        kind = "retryable" if err.retryable else "fatal"
        if isinstance(err, LLMResponseParseError):
            kind = "parse_error"
        spec_model = ""
        try:
            spec_model = self._registry.spec(alias).model
        except LLMError:
            pass
        self._usage.record(
            UsageRecord(
                cycle_id=cycle_id,
                task=task_key,
                model_alias=alias,
                provider=err.provider or "",
                model=spec_model,
                latency_ms=0.0,
                input_tokens=None,
                output_tokens=None,
                status="error",
                error_kind=kind,
            )
        )

    @property
    def usage(self) -> UsageTracker:
        return self._usage
