"""阶段 6:Broker 工厂 fail-safe 测试(指导书 9.4)。

paper 模式绝不创建真实下单客户端(即使 Testnet 密钥齐全);
未知模式 / live 一律拒绝。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from config.loader import (
    AppConfig,
    ConfigError,
    ConfigSet,
    ExecutionSection,
    FallbackConfig,
    LiveSection,
    MarketSection,
    ModelSpec,
    ModelsFile,
    ProviderConfig,
    RiskFile,
    SettingsFile,
    TestnetSection,
    TradingConfig,
    TradingFile,
)
from exchange.factory import build_broker, build_order_manager
from exchange.order_manager import OrderManager
from exchange.paper_broker import PaperBroker

# 组装 ConfigSet 所需的最小合法 ModelsFile(与工厂无关,仅为过校验)
_MIN_MODELS = ModelsFile(
    providers={
        "primary": ProviderConfig(base_url="https://x/v1", api_key="k"),
    },
    models={"fast": ModelSpec(provider="primary", model="m")},
    routing={"decision": "fast"},
    fallback=FallbackConfig(enabled=False),
)


def make_cfg(
    *,
    mode: str = "paper",
    enable_testnet: bool = False,
    testnet_key: str = "",
    testnet_secret: str = "",
    enable_live: bool = False,
    live_key: str = "",
    live_secret: str = "",
    root: Path | None = None,
) -> ConfigSet:
    settings = SettingsFile(
        app=AppConfig(data_dir=str(root / "var")) if root else AppConfig(),
        trading=TradingConfig(
            mode=mode,
            enable_live_trading=enable_live,
            enable_binance_testnet=enable_testnet,
        ),
    )
    trading = TradingFile(
        market=MarketSection(),
        execution=ExecutionSection(
            mode=mode,
            testnet=TestnetSection(api_key=testnet_key, api_secret=testnet_secret),
            live=LiveSection(api_key=live_key, api_secret=live_secret),
        ),
    )
    return ConfigSet(
        settings=settings,
        models_file=_MIN_MODELS,
        trading_file=trading,
        risk_file=RiskFile(),
        project_root=root or Path("."),
    )


class PoisonExchange:
    """任何方法被调用都意味着 paper 模式泄漏到了真实客户端(测试专用毒丸)。"""

    def __getattr__(self, name):
        def boom(*args, **kwargs):
            raise AssertionError(f"paper 模式不得触碰交易所客户端: {name}")

        return boom


# ----------------------------------------------------------------------
# paper:绝不创建真实客户端
# ----------------------------------------------------------------------
def test_paper_builds_paper_broker(tmp_path):
    cfg = make_cfg(mode="paper", root=tmp_path)
    broker = build_broker(cfg, price_provider=lambda s: Decimal("50000"))
    assert isinstance(broker, PaperBroker)


def test_paper_never_creates_client_even_with_testnet_keys(tmp_path):
    """即使 Testnet 密钥齐全,paper 模式也只构建 PaperBroker(9.4 硬约束)。"""
    cfg = make_cfg(
        mode="paper",
        testnet_key="tn-key",
        testnet_secret="tn-secret",
        root=tmp_path,
    )
    # 注入毒丸 exchange:paper 路径必须完全忽略它
    broker = build_broker(
        cfg,
        price_provider=lambda s: Decimal("50000"),
        exchange=PoisonExchange(),
    )
    assert isinstance(broker, PaperBroker)
    assert broker.name == "paper_broker"  # type: ignore[attr-defined]


def test_paper_state_path_default_under_data_dir(tmp_path):
    cfg = make_cfg(mode="paper", root=tmp_path)
    broker = build_broker(cfg, price_provider=lambda s: Decimal("50000"))
    assert broker._path == tmp_path / "var" / "paper_broker.json"  # type: ignore[attr-defined]


# ----------------------------------------------------------------------
# testnet:显式启用 + 独立密钥 + 注入假交易所
# ----------------------------------------------------------------------
def test_testnet_builds_testnet_broker_with_flags_and_keys(tmp_path):
    from exchange.testnet_broker import BinanceTestnetBroker

    cfg = make_cfg(
        mode="testnet",
        enable_testnet=True,
        testnet_key="tn-key",
        testnet_secret="tn-secret",
        root=tmp_path,
    )
    ex = _SandboxFakeExchange()
    broker = build_broker(cfg, exchange=ex)
    assert isinstance(broker, BinanceTestnetBroker)
    assert ex.sandbox is True  # sandbox 在构造时已启用


class _SandboxFakeExchange:
    def __init__(self) -> None:
        self.sandbox: bool | None = None
        self.urls = {"api": {"public": "https://testnet.binance.vision"}}

    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox = enabled


def test_testnet_requires_flag_double_check(tmp_path):
    """配置层已校验;工厂再做双保险(直接构造绕过 loader 也拒绝)。"""
    cfg = make_cfg(
        mode="testnet",
        enable_testnet=True,
        testnet_key="tn-key",
        testnet_secret="tn-secret",
        root=tmp_path,
    )
    cfg.settings.trading.enable_binance_testnet = False  # 模拟绕过配置层
    with pytest.raises(ConfigError, match="ENABLE_BINANCE_TESTNET"):
        build_broker(cfg)


def test_testnet_requires_keys_double_check(tmp_path):
    cfg = make_cfg(
        mode="testnet",
        enable_testnet=True,
        testnet_key="tn-key",
        testnet_secret="tn-secret",
        root=tmp_path,
    )
    cfg.trading_file.execution.testnet.api_key = ""  # 模拟绕过配置层
    cfg.trading_file.execution.testnet.api_secret = ""
    with pytest.raises(ConfigError, match="BINANCE_TESTNET_API_KEY"):
        build_broker(cfg)


def test_unknown_mode_rejected(tmp_path):
    cfg = make_cfg(mode="paper", root=tmp_path)
    cfg.settings.trading.mode = "live"  # 绕过校验模拟未来攻击面
    cfg.trading_file.execution.mode = "live"
    with pytest.raises(ConfigError, match="live"):
        build_broker(cfg)


def test_error_message_never_contains_keys(tmp_path):
    cfg = make_cfg(
        mode="testnet",
        enable_testnet=True,
        testnet_key="super-secret-key",
        testnet_secret="super-secret-secret",
        root=tmp_path,
    )
    # 拿掉开关让工厂报错;报错文本绝不携带密钥
    cfg.settings.trading.enable_binance_testnet = False
    try:
        build_broker(cfg)
    except ConfigError as e:
        assert "super-secret" not in str(e)
    else:  # pragma: no cover
        pytest.fail("应当拒绝")


# ----------------------------------------------------------------------
# OrderManager 工厂
# ----------------------------------------------------------------------
def test_build_order_manager(tmp_path):
    cfg = make_cfg(mode="paper", root=tmp_path)
    broker = build_broker(cfg, price_provider=lambda s: Decimal("50000"))
    manager = build_order_manager(cfg, broker)
    assert isinstance(manager, OrderManager)
    assert manager._path == tmp_path / "var" / "order_intents.json"  # type: ignore[attr-defined]


async def test_factory_passes_proxy_to_default_client(tmp_path):
    """阶段 8:testnet 工厂把 proxy.url 注入客户端(直连网络无需设置)。"""
    cfg = make_cfg(
        mode="testnet",
        enable_testnet=True,
        testnet_key="tn-key",
        testnet_secret="tn-secret",
        root=tmp_path,
    )
    cfg.settings.proxy.url = "http://127.0.0.1:10808"
    broker = build_broker(cfg)  # 不注入 exchange → 默认交易所携带代理
    try:
        assert broker._client._exchange.https_proxy == "http://127.0.0.1:10808"
    finally:
        await broker._client.aclose()


# ----------------------------------------------------------------------
# live(阶段 8,项目所有者显式授权):四重校验,缺一不可
# ----------------------------------------------------------------------
class LiveOKExchange:
    """正式 API URL、从未 sandbox 的假交易所(仅构造期验证用)。"""

    def __init__(self) -> None:
        self.urls = {"api": {"public": "https://api.binance.com"}}
        self.sandbox: bool | None = None

    def set_sandbox_mode(self, flag: bool) -> None:
        self.sandbox = flag


def make_live_cfg(tmp_path, **kw) -> ConfigSet:
    return make_cfg(
        mode="live",
        enable_live=True,
        live_key="lv-key",
        live_secret="lv-secret",
        root=tmp_path,
        **kw,
    )


def test_live_builds_live_broker(tmp_path):
    from exchange.live_broker import BinanceLiveBroker

    cfg = make_live_cfg(tmp_path)
    ex = LiveOKExchange()
    broker = build_broker(cfg, exchange=ex)
    assert isinstance(broker, BinanceLiveBroker)
    assert broker.name == "binance_live_broker"
    assert ex.sandbox is None  # Live 绝不启用 sandbox


def test_live_requires_enable_flag(tmp_path):
    """绕过配置层(构造后篡改)时,工厂双保险仍拒绝。"""
    cfg = make_live_cfg(tmp_path)
    cfg.settings.trading.enable_live_trading = False
    with pytest.raises(ConfigError, match="ENABLE_LIVE_TRADING"):
        build_broker(cfg, exchange=LiveOKExchange())


def test_live_requires_keys(tmp_path):
    """绕过配置层(构造后篡改)时,工厂双保险仍拒绝缺密钥。"""
    cfg = make_live_cfg(tmp_path)
    cfg.trading_file.execution.live.api_key = ""
    with pytest.raises(ConfigError, match="BINANCE_LIVE_API_KEY"):
        build_broker(cfg, exchange=LiveOKExchange())


def test_live_requires_risk_enabled(tmp_path):
    cfg = make_live_cfg(tmp_path)
    cfg.risk_file.risk.enabled = False  # 模拟绕过配置层
    with pytest.raises(ConfigError, match="risk.enabled"):
        build_broker(cfg, exchange=LiveOKExchange())


def test_live_rejects_testnet_pointed_url(tmp_path):
    """allow_live 后 URL 仍指向 Testnet → 拒绝(绝不误连)。"""
    from exchange.binance_client import TestnetSafetyError

    cfg = make_live_cfg(tmp_path)

    class SandboxedExchange(LiveOKExchange):
        def set_sandbox_mode(self, flag: bool) -> None:
            self.sandbox = flag
            self.urls = {
                "api": {"public": "https://testnet.binance.vision/api/v3"}
            }

    # 注入的 exchange 已被"上游"切到 Testnet URL(模拟配置串线)
    ex = SandboxedExchange()
    ex.set_sandbox_mode(True)
    with pytest.raises(TestnetSafetyError, match="Testnet"):
        build_broker(cfg, exchange=ex)


def test_paper_ignores_live_keys_and_exchange(tmp_path):
    """paper 模式即使 Live 密钥齐全也绝不触碰任何交易所客户端。"""
    cfg = make_cfg(
        mode="paper",
        live_key="lv-key",
        live_secret="lv-secret",
        root=tmp_path,
    )
    broker = build_broker(
        cfg, price_provider=lambda s: Decimal("50000"), exchange=PoisonExchange()
    )
    assert isinstance(broker, PaperBroker)
