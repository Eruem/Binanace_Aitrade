"""Binance Live(真实资金)Broker(阶段 8,项目所有者显式授权)。

复用 BinanceTestnetBroker 的全部订单/对账逻辑(该逻辑与环境无关),
唯一区别是名称与上游客户端:客户端必须经显式 allow_live + 正式 API
URL 确认后才允许连接账户。

安全前提(缺一不可,配置层与工厂双重校验):
- TRADING_MODE=live 且 ENABLE_LIVE_TRADING=true;
- BINANCE_LIVE_API_KEY / BINANCE_LIVE_API_SECRET 独立提供(禁止复用 Testnet);
- risk.enabled 强制为 true(风控引擎不可绕过)。
"""

from __future__ import annotations

from .testnet_broker import BinanceTestnetBroker


class BinanceLiveBroker(BinanceTestnetBroker):
    """Live Spot Broker;订单逻辑与 Testnet 相同,资金为真实资产。"""

    name = "binance_live_broker"
