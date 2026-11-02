"""Broker 工厂(阶段 6 Testnet;阶段 8 Live)。

按交易模式构建 Broker,fail-safe:
- paper → PaperBroker;**绝不**创建任何交易所客户端、绝不触碰密钥
  (即使 testnet/live 密钥存在也一律忽略);
- testnet → 校验显式开关与独立 Testnet 密钥后构建
  BinanceTestnetBroker(sandbox 前置 + Testnet URL 确认);
- live(真实资金,项目所有者显式授权)→ 模式 + ENABLE_LIVE_TRADING=true
  + 独立 Live 密钥 + risk.enabled=true 四重校验后构建 BinanceLiveBroker
  (绝不 sandbox + 正式 API URL 确认);
- 其他模式 → 拒绝。
"""

from __future__ import annotations

from pathlib import Path

from config.loader import ConfigError, ConfigSet

from .interfaces import Broker
from .order_manager import OrderManager
from .paper_broker import PaperBroker


def build_broker(
    cfg: ConfigSet,
    *,
    price_provider=None,
    paper_state_path: str | Path | None = None,
    exchange=None,
) -> Broker:
    """按 cfg 构建 Broker。exchange 仅供测试注入假交易所。"""
    mode = cfg.trading_mode

    if mode == "paper":
        # 安全硬约束:paper 模式禁止创建真实下单客户端(指导书 9.4);
        # exchange 注入参数在此被有意忽略
        state_path = paper_state_path or (
            Path(cfg.settings.app.data_dir) / "paper_broker.json"
        )
        return PaperBroker(price_provider=price_provider, state_path=state_path)

    if mode == "testnet":
        if not cfg.settings.trading.enable_binance_testnet:  # 双保险(配置已校验)
            raise ConfigError("testnet 模式要求 ENABLE_BINANCE_TESTNET=true")
        tn = cfg.trading_file.execution.testnet
        if not tn.api_key or not tn.api_secret:
            raise ConfigError(
                "testnet 模式缺少 Testnet API Key/Secret"
                "(BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET)"
            )
        from .binance_client import BinanceSpotClient  # 局部导入:paper 路径零耦合
        from .testnet_broker import BinanceTestnetBroker

        client = BinanceSpotClient(
            tn.api_key,
            tn.api_secret,
            testnet=True,
            exchange=exchange,
            proxy_url=cfg.settings.proxy.url or None,
        )
        return BinanceTestnetBroker(client)

    if mode == "live":
        # 阶段 8:真实资金——四重显式校验(模式 + 开关 + 独立密钥 + 强制风控);
        # 每一条在配置层已校验,此处为工厂级双保险(纵深防御)
        if not cfg.settings.trading.enable_live_trading:
            raise ConfigError("live 模式要求 ENABLE_LIVE_TRADING=true")
        if not cfg.risk_file.risk.enabled:
            raise ConfigError("live 模式要求 risk.enabled=true(强制风控)")
        lv = cfg.trading_file.execution.live
        if not lv.api_key or not lv.api_secret:
            raise ConfigError(
                "live 模式缺少 Live API Key/Secret"
                "(BINANCE_LIVE_API_KEY / BINANCE_LIVE_API_SECRET)"
            )
        from .binance_client import BinanceSpotClient  # 局部导入:paper 路径零耦合
        from .live_broker import BinanceLiveBroker

        client = BinanceSpotClient(
            lv.api_key,
            lv.api_secret,
            testnet=False,
            allow_live=True,
            exchange=exchange,
            proxy_url=cfg.settings.proxy.url or None,
        )
        return BinanceLiveBroker(client)

    raise ConfigError(f"未知交易模式: {mode!r}(仅允许 paper/testnet/live)")


def build_order_manager(cfg: ConfigSet, broker: Broker) -> OrderManager:
    """订单意图持久化路径统一放 data_dir 下。"""
    state_path = Path(cfg.settings.app.data_dir) / "order_intents.json"
    return OrderManager(broker, state_path=state_path)
