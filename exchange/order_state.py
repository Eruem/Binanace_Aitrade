"""订单状态机(阶段 5,指导书 9.2)。

10 个订单状态与合法流转表;任何状态跳变必须经 assert_transition 校验,
不允许的流转抛 InvalidTransitionError(宁可报错,不许悄悄跳状态)。

约定:
- CREATED / RISK_APPROVED / SUBMITTING 是"进入交易所之前"的本地状态;
- NEW / PARTIALLY_FILLED / FILLED / CANCELED / REJECTED / EXPIRED 是
  交易所确认后的状态;
- UNKNOWN 表示提交结果未知(超时/断线),恢复只能查询原单,
  未确认前不得生成新 Client Order ID 重发(指导书 9.3)。
"""

from __future__ import annotations

from enum import Enum


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTING = "SUBMITTING"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


# 合法流转表(单向;终态不允许任何流出)
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset(
        {OrderStatus.RISK_APPROVED, OrderStatus.REJECTED, OrderStatus.CANCELED}
    ),
    OrderStatus.RISK_APPROVED: frozenset(
        {OrderStatus.SUBMITTING, OrderStatus.REJECTED, OrderStatus.CANCELED}
    ),
    # SUBMITTING -> RISK_APPROVED:确认未送达交易所后回退到待提交(恢复路径)
    OrderStatus.SUBMITTING: frozenset(
        {
            OrderStatus.NEW,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.UNKNOWN,
            OrderStatus.RISK_APPROVED,
        }
    ),
    OrderStatus.NEW: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    # UNKNOWN -> RISK_APPROVED:查询确认交易所无此单后,允许同一 ID 重试
    OrderStatus.UNKNOWN: frozenset(
        {
            OrderStatus.NEW,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.RISK_APPROVED,
        }
    ),
}

# 终态:不会再变化的订单
TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
)


class InvalidTransitionError(Exception):
    """非法状态流转(不允许的订单状态跳变)。"""


def can_transition(src: OrderStatus, dst: OrderStatus) -> bool:
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


def assert_transition(src: OrderStatus, dst: OrderStatus) -> None:
    if not can_transition(src, dst):
        raise InvalidTransitionError(f"非法订单状态流转: {src.value} -> {dst.value}")


def is_terminal(status: OrderStatus) -> bool:
    return status in TERMINAL_STATUSES
