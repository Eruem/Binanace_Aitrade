"""配置加载与校验(阶段 0)。

职责:
- 读取 config/*.yaml;
- 解析 ${VAR} / ${VAR:-default} 环境变量插值(PyyAML 不会自动做这件事);
- 用 Pydantic 校验结构与安全约束;
- 缺少必需配置时抛出 ConfigError,错误信息只包含变量名,绝不包含变量值;
- 对疑似密钥字段提供脱敏工具,禁止出现在日志/异常里。

安全约束(不可绕过):
- 第一阶段 enable_live_trading 必须为 false;
- trading.mode 只允许 paper / testnet;
- testnet 模式必须显式设置 ENABLE_BINANCE_TESTNET=true。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_SECRET_KEY_HINTS = ("api_key", "secret", "token", "password", "authorization")


class ConfigError(Exception):
    """配置缺失或非法。信息中不得出现任何密钥值。"""


# ---------------------------------------------------------------------------
# Pydantic 配置模型
# ---------------------------------------------------------------------------

class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "ai-crypto-trading-agent"
    env: str = "development"
    log_level: str = "INFO"
    data_dir: str = "var"


class TradingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "paper"
    enable_live_trading: bool = False
    enable_binance_testnet: bool = False

    @field_validator("mode")
    @classmethod
    def _mode_allowed(cls, v: str) -> str:
        if v not in ("paper", "testnet", "live"):
            raise ValueError(
                f"trading.mode 只允许 paper/testnet/live,收到 {v!r}"
            )
        return v


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_dir: str = "config"
    usage_log: str = "var/usage/usage.jsonl"
    audit_log: str = "var/audit/audit.jsonl"


class DatabaseSection(BaseModel):
    """数据库(阶段 7,可选)。URL 为空 = 不启用(仅本地 JSONL 审计)。

    scheme 校验在 ConfigSet 层做(报错只含 scheme 名,不回显完整 URL)。
    """

    model_config = ConfigDict(extra="forbid")

    url: str = ""

    @field_validator("url", mode="before")
    @classmethod
    def _yaml_empty_is_str(cls, v: Any) -> Any:
        return "" if v is None else v

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def scheme(self) -> str:
        return self.url.split(":", 1)[0] if self.url else ""

    def masked(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "url": mask_db_url(self.url)}


class TelegramSection(BaseModel):
    """Telegram 通知(阶段 7,可选)。仅发送通知,不接收聊天指令。"""

    model_config = ConfigDict(extra="forbid")

    bot_token: str = ""
    chat_id: str = ""

    @field_validator("bot_token", "chat_id", mode="before")
    @classmethod
    def _yaml_empty_is_str(cls, v: Any) -> Any:
        # chat_id 纯数字会被 YAML 解析为 int,统一转字符串
        if v is None:
            return ""
        return str(v)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def masked(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "bot_token": mask_secret(self.bot_token) if self.bot_token else "",
            "chat_id": mask_secret(self.chat_id) if self.chat_id else "",
        }


class ProxySection(BaseModel):
    """交易所流量代理(可选,阶段 8)。

    CCXT 默认 trust_env=False(刻意不读环境/系统代理),因此被墙网络
    需要显式配置;为空 = 直连。scheme 校验在 ConfigSet 层做,
    报错只含 scheme 名,不回显完整 URL(可能含代理账号口令)。
    """

    model_config = ConfigDict(extra="forbid")

    url: str = ""

    @field_validator("url", mode="before")
    @classmethod
    def _yaml_empty_is_str(cls, v: Any) -> Any:
        return "" if v is None else v

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def scheme(self) -> str:
        return self.url.split(":", 1)[0] if self.url else ""

    def masked(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "url": mask_proxy_url(self.url)}


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key: str
    timeout: int = 45

    def masked(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "api_key": mask_secret(self.api_key),
            "timeout": self.timeout,
        }


class ModelSpec(BaseModel):
    """一个模型别名的完整定义(指导书 5.1)。"""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    temperature: float = 0.1
    max_tokens: int = 2000
    timeout: int = 45
    supports_json: bool = False


class FallbackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    model: str = "backup"
    max_attempts: int = 2
    on_all_models_failed: str = "HOLD"

    @field_validator("on_all_models_failed")
    @classmethod
    def _hold_only(cls, v: str) -> str:
        if v != "HOLD":
            raise ValueError("on_all_models_failed 只允许 HOLD")
        return v


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_calls_per_cycle: int = 8
    max_concurrent_requests: int = 3
    complexity_threshold: int = 3


class SafetyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on_all_models_failed: str = "HOLD"

    @field_validator("on_all_models_failed")
    @classmethod
    def _hold_only(cls, v: str) -> str:
        if v != "HOLD":
            raise ValueError("safety.on_all_models_failed 只允许 HOLD;LLM 不得改变该值")
        return v


class ModelsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderConfig]
    models: dict[str, ModelSpec]
    routing: dict[str, str]
    fallback: FallbackConfig = FallbackConfig()
    limits: LimitsConfig = LimitsConfig()
    safety: SafetyConfig = SafetyConfig()

    @field_validator("routing")
    @classmethod
    def _routing_targets_exist(cls, v: dict[str, Any], info: Any) -> dict[str, Any]:
        return v  # 目标别名在 ConfigSet 中结合 models 再校验


class MarketSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exchange: str = "binance"
    market_type: str = "spot"
    default_symbol: str = "BTC/USDT"
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT"])
    timeframes: list[str] = Field(default_factory=lambda: ["15m", "1h", "4h"])

    @field_validator("market_type")
    @classmethod
    def _spot_only(cls, v: str) -> str:
        if v != "spot":
            raise ValueError("第一阶段仅允许 spot;禁止合约/杠杆")
        return v


class StrategySection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[str] = Field(default_factory=list)
    research_only: list[str] = Field(default_factory=list)


class TestnetSection(BaseModel):
    """Binance Spot Testnet 密钥(与任何正式密钥分开;只经环境变量提供)。"""

    model_config = ConfigDict(extra="forbid")

    api_key: str = ""
    api_secret: str = ""

    @field_validator("api_key", "api_secret", mode="before")
    @classmethod
    def _yaml_empty_is_str(cls, v: Any) -> Any:
        # `${VAR:-}` 插值为空时 YAML 会解析为 None,归一化为空串
        return "" if v is None else v

    def masked(self) -> dict[str, Any]:
        return {
            "api_key": mask_secret(self.api_key),
            "api_secret": mask_secret(self.api_secret),
        }


class LiveSection(BaseModel):
    """Live 真实资金密钥(阶段 8;仅 TRADING_MODE=live 时使用)。

    与 Testnet 密钥完全分开(禁止复用),只经环境变量提供。
    """

    model_config = ConfigDict(extra="forbid")

    api_key: str = ""
    api_secret: str = ""

    @field_validator("api_key", "api_secret", mode="before")
    @classmethod
    def _yaml_empty_is_str(cls, v: Any) -> Any:
        return "" if v is None else v

    def masked(self) -> dict[str, Any]:
        return {
            "api_key": mask_secret(self.api_key),
            "api_secret": mask_secret(self.api_secret),
        }


class ExecutionSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "paper"
    broker: str | None = None  # 可选镜像字段;省略时由 mode 派生
    testnet: TestnetSection = TestnetSection()
    live: LiveSection = LiveSection()

    @property
    def derived_broker(self) -> str:
        return {
            "paper": "paper_broker",
            "testnet": "binance_testnet_broker",
            "live": "binance_live_broker",
        }[self.mode]


class TradingFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: MarketSection = MarketSection()
    strategies: StrategySection = StrategySection()
    execution: ExecutionSection = ExecutionSection()


class SettingsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: AppConfig = AppConfig()
    trading: TradingConfig = TradingConfig()
    paths: PathsConfig = PathsConfig()
    database: DatabaseSection = DatabaseSection()
    telegram: TelegramSection = TelegramSection()
    proxy: ProxySection = ProxySection()


class RiskParams(BaseModel):
    """风控参数(指导书 8.2)。修改限额只能改配置文件,LLM 无权干预。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_single_order_equity_pct: float = Field(default=0.10, gt=0.0, le=1.0)
    max_total_exposure_pct: float = Field(default=0.30, gt=0.0, le=1.0)
    max_single_trade_risk_pct: float = Field(default=0.005, gt=0.0, le=1.0)
    max_daily_loss_pct: float = Field(default=0.02, gt=0.0, le=1.0)
    max_consecutive_losses: int = Field(default=3, ge=1)
    reject_expired_signal: bool = True
    reject_duplicate_order: bool = True
    require_closed_candle: bool = True
    stop_on_data_error: bool = True
    assumed_stop_loss_pct: float = Field(default=0.02, gt=0.0, le=1.0)
    fee_rate: float = Field(default=0.001, ge=0.0, le=0.1)
    slippage_rate: float = Field(default=0.0005, ge=0.0, le=0.1)
    min_order_usdt: float = Field(default=5.0, gt=0.0)


class RiskFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk: RiskParams = RiskParams()


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def mask_secret(value: str) -> str:
    """密钥脱敏:任何情况下都不输出原值或片段。"""
    if not value:
        return "(missing)"
    return "****"


_DB_CRED_PATTERN = re.compile(r"(://[^/@\s]+):[^@/\s]*@")

# 代理 URL 整段 userinfo 都可能就是凭据,直接全遮
_PROXY_USERINFO_PATTERN = re.compile(r"(://)[^/@\s]*@")


def mask_db_url(url: str) -> str:
    """数据库 URL 脱敏:隐藏 user:password,保留 scheme/host/库名。"""
    if not url:
        return "(disabled)"
    return _DB_CRED_PATTERN.sub(r"\1:****@", url)


def mask_proxy_url(url: str) -> str:
    """代理 URL 脱敏:隐藏整个 userinfo(可能就是账号口令),保留 host:port。"""
    if not url:
        return "(direct)"
    return _PROXY_USERINFO_PATTERN.sub(r"\1****@", url)


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)


def interpolate_env(
    raw: str, env: dict[str, str], *, source: str, missing: list[str]
) -> str:
    """解析 ${VAR} 与 ${VAR:-default};缺变量时记录变量名(不记录值)。"""

    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        if name in env and env[name] != "":
            return env[name]
        if default is not None:
            return default
        missing.append(f"{source}: 环境变量 {name} 未设置")
        return ""

    return _ENV_PATTERN.sub(repl, raw)


def _load_yaml_with_env(
    path: Path, env: dict[str, str], missing: list[str]
) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"配置文件不存在: {path}")
    text = path.read_text(encoding="utf-8")
    interpolated = interpolate_env(text, env, source=path.name, missing=missing)
    try:
        data = yaml.safe_load(interpolated) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"配置文件 YAML 解析失败: {path.name}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件根节点必须是映射: {path.name}")
    return data


# ---------------------------------------------------------------------------
# 配置集合
# ---------------------------------------------------------------------------

class ConfigSet:
    """一次加载后的完整配置。"""

    def __init__(
        self,
        *,
        settings: SettingsFile,
        models_file: ModelsFile,
        trading_file: TradingFile,
        risk_file: RiskFile,
        project_root: Path,
    ) -> None:
        self.settings = settings
        self.models_file = models_file
        self.trading_file = trading_file
        self.risk_file = risk_file
        self.project_root = project_root
        self._validate_cross_fields()

    def _validate_cross_fields(self) -> None:
        mf = self.models_file
        # routing 指向的别名必须存在
        for task, alias in mf.routing.items():
            if alias not in mf.models:
                raise ConfigError(
                    f"routing.{task} 指向未定义的模型别名 {alias!r}"
                )
        # fallback 别名必须存在
        if mf.fallback.enabled and mf.fallback.model not in mf.models:
            raise ConfigError(
                f"fallback.model 指向未定义的模型别名 {mf.fallback.model!r}"
            )
        # 每个别名的 provider 必须存在
        for alias, spec in mf.models.items():
            if spec.provider not in mf.providers:
                raise ConfigError(
                    f"models.{alias}.provider 指向未定义的 provider {spec.provider!r}"
                )
        # 安全硬约束(阶段 8 修订,项目所有者授权:Live 仅显式开启时可用;
        # 默认 paper/testnet 下 true 依旧拒绝,延续 fail-safe 默认)
        tr = self.settings.trading
        if tr.enable_live_trading and tr.mode != "live":
            raise ConfigError(
                "ENABLE_LIVE_TRADING=true 仅允许配合 TRADING_MODE=live;"
                "paper/testnet 模式必须为 false"
            )
        if tr.mode == "live" and not tr.enable_live_trading:
            raise ConfigError(
                "TRADING_MODE=live 要求同时设置 ENABLE_LIVE_TRADING=true"
            )
        if tr.mode == "testnet" and not tr.enable_binance_testnet:
            raise ConfigError(
                "TRADING_MODE=testnet 要求同时设置 ENABLE_BINANCE_TESTNET=true"
            )
        if tr.enable_binance_testnet and tr.mode != "testnet":
            raise ConfigError(
                "ENABLE_BINANCE_TESTNET=true 要求 TRADING_MODE=testnet"
            )
        # 阶段 6:trading.yaml 的 execution.mode 必须与环境变量驱动的
        # trading.mode 一致(配置意外指向错误环境在启动时拒绝)
        ex = self.trading_file.execution
        if ex.mode != tr.mode:
            raise ConfigError(
                f"trading.yaml execution.mode({ex.mode!r}) 与 "
                f"trading.mode({tr.mode!r}) 不一致;以环境变量 TRADING_MODE 为准"
            )
        # broker 由 mode 派生;若显式填写必须一致(防止镜像字段说谎)
        expected_broker = ex.derived_broker
        if ex.broker is not None and ex.broker != expected_broker:
            raise ConfigError(
                f"execution.broker={ex.broker!r} 与 mode={ex.mode!r} 不一致,"
                f"应为 {expected_broker!r}(或省略该字段)"
            )
        # 阶段 6:testnet 模式必须提供独立 Testnet 密钥(缺一不可,报错只含变量名)
        if tr.mode == "testnet" and (not ex.testnet.api_key or not ex.testnet.api_secret):
            raise ConfigError(
                "testnet 模式缺少 Testnet API Key/Secret;"
                "请在 .env 设置 BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET"
            )
        # 阶段 8:live 模式必须提供独立 Live 密钥(与 Testnet 分开,禁止复用)
        if tr.mode == "live" and (not ex.live.api_key or not ex.live.api_secret):
            raise ConfigError(
                "live 模式缺少 Live API Key/Secret;"
                "请在 .env 设置 BINANCE_LIVE_API_KEY / BINANCE_LIVE_API_SECRET"
            )
        # 阶段 8:真实资金模式强制开启风控引擎(不允许配置关闭)
        if tr.mode == "live" and not self.risk_file.risk.enabled:
            raise ConfigError(
                "live(真实资金)模式强制要求 risk.enabled=true;"
                "不允许关闭风控引擎"
            )
        # 阶段 7:数据库 URL scheme 白名单(报错只含 scheme 名,不回显 URL)
        db = self.settings.database
        if db.enabled and not db.url.startswith(
            ("postgresql+psycopg://", "postgresql://", "sqlite://")
        ):
            raise ConfigError(
                f"database.url 只允许 postgresql/SQLite,收到 {db.scheme()!r};"
                "口令请写在 URL 内并只经环境变量 DATABASE_URL 提供"
            )
        # 阶段 7:Telegram 必须成对配置(只发送通知;缺一个视为未启用或报错)
        tg = self.settings.telegram
        if bool(tg.bot_token) != bool(tg.chat_id):
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN 与 TELEGRAM_CHAT_ID 必须同时设置"
                "(只发送通知;两者都留空则禁用)"
            )
        # 阶段 8:交易所代理 URL 只允许 http/https(报错只含 scheme,不回显 URL)
        px = self.settings.proxy
        if px.enabled and not px.url.startswith(("http://", "https://")):
            raise ConfigError(
                f"EXCHANGE_PROXY_URL 只允许 http:// 或 https://,收到 scheme "
                f"{px.scheme()!r};代理凭据写在 URL 内且只经环境变量提供"
            )

    # 便捷访问 -------------------------------------------------------------
    @property
    def trading_mode(self) -> str:
        return self.settings.trading.mode

    @property
    def models(self) -> dict[str, ModelSpec]:
        return self.models_file.models

    @property
    def providers(self) -> dict[str, ProviderConfig]:
        return self.models_file.providers

    @property
    def routing(self) -> dict[str, str]:
        return self.models_file.routing

    @property
    def fallback(self) -> FallbackConfig:
        return self.models_file.fallback

    @property
    def limits(self) -> LimitsConfig:
        return self.models_file.limits

    @property
    def risk(self) -> RiskParams:
        return self.risk_file.risk

    def sanitized_summary(self) -> dict[str, Any]:
        """给 check_config / 启动横幅用的脱敏摘要。"""
        return {
            "app": self.settings.app.model_dump(),
            "trading_mode": self.trading_mode,
            "enable_live_trading": False,
            "enable_binance_testnet": self.settings.trading.enable_binance_testnet,
            "providers": {
                name: p.masked() for name, p in self.models_file.providers.items()
            },
            "models": {
                alias: spec.model_dump() for alias, spec in self.models_file.models.items()
            },
            "routing": dict(self.models_file.routing),
            "fallback": self.models_file.fallback.model_dump(),
            "limits": self.models_file.limits.model_dump(),
            "symbols": list(self.trading_file.market.symbols),
            "timeframes": list(self.trading_file.market.timeframes),
            "risk": self.risk_file.risk.model_dump(),
            "execution": {
                "mode": self.trading_file.execution.mode,
                "broker": self.trading_file.execution.derived_broker,
                "testnet": self.trading_file.execution.testnet.masked(),
                "live": self.trading_file.execution.live.masked(),
            },
            "database": self.settings.database.masked(),
            "telegram": self.settings.telegram.masked(),
            "proxy": self.settings.proxy.masked(),
        }


def _build_env(project_root: Path) -> dict[str, str]:
    """合并进程环境与 .env(进程环境优先);.env 只在本地存在,永不提交。"""
    env = {k: v for k, v in os.environ.items() if v is not None}
    env_file = project_root / ".env"
    if env_file.exists():
        for k, v in dotenv_values(env_file).items():
            if v is not None and k not in env:
                env[k] = v
    return env


def load_config(
    config_dir: str | Path | None = None, *, project_root: str | Path | None = None
) -> ConfigSet:
    """加载并校验全部配置。

    Args:
        config_dir: 配置目录;默认 <project_root>/config。
        project_root: 项目根;默认本文件所在目录的上一级。
    """
    root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent
    cfg_dir = Path(config_dir) if config_dir else root / "config"

    env = _build_env(root)
    missing: list[str] = []

    settings_raw = _load_yaml_with_env(cfg_dir / "settings.yaml", env, missing)
    models_raw = _load_yaml_with_env(cfg_dir / "models.yaml", env, missing)
    trading_raw = _load_yaml_with_env(cfg_dir / "trading.yaml", env, missing)
    risk_raw = _load_yaml_with_env(cfg_dir / "risk.yaml", env, missing)

    if missing:
        bullet = "\n  - ".join(missing)
        raise ConfigError(f"缺少必需配置:\n  - {bullet}\n请参考 .env.example 在本地 .env 中设置")

    try:
        settings = SettingsFile.model_validate(settings_raw)
        models_file = ModelsFile.model_validate(models_raw)
        trading_file = TradingFile.model_validate(trading_raw)
        risk_file = RiskFile.model_validate(risk_raw)
    except ConfigError:
        raise
    except Exception as e:  # pydantic ValidationError 等
        raise ConfigError(f"配置校验失败: {e}") from e

    return ConfigSet(
        settings=settings,
        models_file=models_file,
        trading_file=trading_file,
        risk_file=risk_file,
        project_root=root,
    )
