"""执行网关(阶段 5/6,指导书 9)。

- interfaces:Broker 接口 + OrderResult / TradeRecord;
- order_state:订单状态机(10 状态 + 合法流转表);
- paper_broker:Paper Broker(本地模拟成交,默认);
- order_manager:Client Order ID 幂等、意图持久化、UNKNOWN 恢复;
- binance_client / testnet_broker:Binance Spot Testnet(阶段 6,显式启用);
- live_broker:Live 真实资金 Broker(阶段 8,项目所有者显式授权);
- order_reconciler:订单与交易所定期对账,不一致自动停止新开仓;
- factory:按交易模式构建 Broker(paper 绝不创建真实客户端)。

本包不持有任何 API Key;密钥只经配置注入客户端内部。
"""

from .binance_client import (
    BinanceSpotClient,
    ExchangeAPIError,
    ExchangeNetworkError,
    SymbolRules,
    TestnetSafetyError,
)
from .factory import build_broker, build_order_manager
from .interfaces import (
    Broker,
    BrokerError,
    OrderResult,
    PaperBrokerStateError,
    TradeRecord,
)
from .order_manager import OrderManager, OrderManagerError, make_client_order_id
from .order_reconciler import OrderReconciler, ReconcileReport
from .order_state import (
    InvalidTransitionError,
    OrderStatus,
    assert_transition,
    can_transition,
    is_terminal,
)
from .live_broker import BinanceLiveBroker
from .paper_broker import PaperBroker
from .testnet_broker import BinanceTestnetBroker

__all__ = [
    "Broker",
    "BrokerError",
    "BinanceSpotClient",
    "BinanceTestnetBroker",
    "ExchangeAPIError",
    "ExchangeNetworkError",
    "InvalidTransitionError",
    "OrderManager",
    "OrderManagerError",
    "OrderReconciler",
    "OrderResult",
    "OrderStatus",
    "PaperBroker",
    "PaperBrokerStateError",
    "ReconcileReport",
    "SymbolRules",
    "TestnetSafetyError",
    "TradeRecord",
    "assert_transition",
    "build_broker",
    "build_order_manager",
    "can_transition",
    "is_terminal",
    "make_client_order_id",
]
