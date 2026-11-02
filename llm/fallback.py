"""备用模型策略(指导书 5.4)。

主模型 retryable 失败可重试;主模型 fatal 或重试耗尽时切换到备用模型。
全部失败 -> 安全错误(HOLD),绝不产生交易指令。
切换决策由 Router 依据 LLMError.retryable 执行,本模块只提供策略数据。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FallbackPolicy:
    enabled: bool = True
    model_alias: str = "backup"
    max_attempts: int = 2
    on_all_models_failed: str = "HOLD"

    def __post_init__(self) -> None:
        if self.on_all_models_failed != "HOLD":
            raise ValueError("on_all_models_failed 只允许 HOLD")
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须 >= 1")

    @classmethod
    def from_config(cls, cfg: object) -> "FallbackPolicy":
        return cls(
            enabled=bool(getattr(cfg, "enabled", True)),
            model_alias=str(getattr(cfg, "model", "backup")),
            max_attempts=int(getattr(cfg, "max_attempts", 2)),
            on_all_models_failed=str(getattr(cfg, "on_all_models_failed", "HOLD")),
        )

    def candidate_chain(self, primary_alias: str) -> list[str]:
        """返回按顺序尝试的模型别名列表。"""
        chain = [primary_alias]
        if self.enabled and self.model_alias != primary_alias:
            chain.append(self.model_alias)
        return chain
