"""Broker 接口与订单/成交结果(阶段 5,指导书 9.1/6.2)。

业务层只通过 Broker 接口下单;具体实现:
- PaperBroker(阶段 5,本地模拟成交,默认);
- BinanceTestnetBroker(阶段 6,显式启用,独立 Testnet 密钥)。

安全边界:
- 接口本身不接触任何 API Key(凭据只在阶段 6 的 Testnet 实现内部持有);
- Client Order ID 由 OrderManager 生成并保证幂等(同一 decision 只对应一个 ID);
- 交易所返回什么就记录什么:OrderResult 是执行层与业务层之间的最小事实。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from risk.order_validator import OrderRequest

from .order_state import OrderStatus


class OrderResult(BaseModel):
    """一次订单提交/查询的结果(指导书 9.3 要求的记录字段)。"""

    model_config = ConfigDict(extra="forbid")

    client_order_id: str
    exchange_order_id: str | None = None  # Paper 为 paper-N;提交失败为 None
    decision_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    status: OrderStatus
    amount_usdt: Decimal  # 请求的名义金额
    requested_qty: Decimal  # 按成交价折算的基础资产数量
    filled_qty: Decimal = Decimal("0")
    avg_price: Decimal | None = None  # 加权平均成交价
    fee: Decimal = Decimal("0")
    fee_currency: str = "USDT"
    created_at: int  # UTC ms
    updated_at: int  # UTC ms
    error: str | None = None


class TradeRecord(BaseModel):
    """一笔成交(部分成交会产生多笔)。"""

    model_config = ConfigDict(extra="forbid")

    trade_id: str
    client_order_id: str
    exchange_order_id: str | None = None
    decision_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal = Field(ge=0)
    fee_currency: str = "USDT"
    executed_at: int  # UTC ms


class BrokerError(Exception):
    """执行层错误(状态文件损坏、非法调用等)。"""


class PaperBrokerStateError(BrokerError):
    """Paper Broker 状态文件损坏:拒绝交易(无法保证幂等)。"""


class Broker(ABC):
    """Broker 最小接口:提交、查询、撤销、账户快照。"""

    name: str = "broker"

    @abstractmethod
    async def submit_order(
        self, request: OrderRequest, *, client_order_id: str
    ) -> OrderResult:
        """提交订单。同一 client_order_id 必须幂等(绝不重复记账)。"""

    @abstractmethod
    async def get_order(
        self, client_order_id: str, *, symbol: str | None = None
    ) -> OrderResult | None:
        """按 Client Order ID 查询;不存在返回 None(用于 UNKNOWN 恢复)。

        symbol:真实交易所(阶段 6)按 Client Order ID 查询必须带交易对,
        由 OrderManager 从订单意图传入;Paper 实现忽略该参数。
        """

    @abstractmethod
    async def cancel_order(
        self, client_order_id: str, *, symbol: str | None = None
    ) -> OrderResult:
        """撤销未成交订单(仅 NEW / PARTIALLY_FILLED 可撤销)。"""

    @abstractmethod
    async def get_account(self) -> object:
        """账户快照(阶段 5/6 供风控使用,risk.engine.AccountSnapshot)。"""
