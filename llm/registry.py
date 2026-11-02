"""Model Registry(指导书 5.1)。

Agent 不得直接实例化任何模型客户端;只能通过 ModelRouter 按别名调用。
Registry 负责把配置里的别名(fast/reasoning/backup...)解析成
ModelSpec + 对应 Provider 实例。
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import LLMFatalError, LLMProvider
from .providers import OpenAICompatibleProvider
from .schemas import LLMResponse, TaskType


class ModelRegistry:
    """别名 -> (ModelSpec, Provider) 的只读注册表。"""

    def __init__(
        self,
        *,
        providers_config: dict[str, Any],
        models_config: dict[str, Any],
        provider_factory: Any = None,
    ) -> None:
        """Args:
            providers_config: {name: {base_url, api_key, timeout}}
            models_config: {alias: ModelSpec-like dict}
            provider_factory: 可注入的工厂(测试用);默认构造
                OpenAICompatibleProvider。签 factory(name, cfg) -> LLMProvider。
        """
        self._specs: dict[str, Any] = {}
        self._providers: dict[str, LLMProvider] = {}
        factory = provider_factory or self._default_factory
        for name, pcfg in providers_config.items():
            self._providers[name] = factory(name, pcfg)
        for alias, spec in models_config.items():
            if spec.provider not in self._providers:
                raise LLMFatalError(
                    f"models.{alias}.provider 指向未定义的 provider {spec.provider!r}"
                )
            self._specs[alias] = spec

    @staticmethod
    def _default_factory(name: str, cfg: Any) -> LLMProvider:
        return OpenAICompatibleProvider(
            name,
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            timeout=float(cfg.timeout),
        )

    def aliases(self) -> list[str]:
        return sorted(self._specs)

    def spec(self, alias: str) -> Any:
        try:
            return self._specs[alias]
        except KeyError:
            raise LLMFatalError(f"未注册的模型别名: {alias!r}") from None

    def provider_for(self, alias: str) -> LLMProvider:
        spec = self.spec(alias)
        return self._providers[spec.provider]

    async def aclose(self) -> None:
        for p in self._providers.values():
            await p.aclose()

    @classmethod
    def from_configset(cls, configset: Any, **kwargs: Any) -> "ModelRegistry":
        """从 config.ConfigSet 构建(阶段 1 便捷入口)。"""
        mf = configset.models_file
        return cls(
            providers_config=mf.providers,
            models_config=mf.models,
            **kwargs,
        )


class ModelHandle:
    """Router 内部使用的一次调用目标(别名 + 规格)。"""

    __slots__ = ("alias", "spec")

    def __init__(self, alias: str, spec: Any) -> None:
        self.alias = alias
        self.spec = spec


__all__ = ["ModelRegistry", "ModelHandle", "LLMResponse", "TaskType"]
