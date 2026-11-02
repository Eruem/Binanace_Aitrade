"""阶段 5:订单状态机测试(指导书 9.2)。

全部离线:只测状态流转表,不涉及任何交易所/网络。
"""

from __future__ import annotations

import pytest

from exchange.order_state import (
    InvalidTransitionError,
    OrderStatus,
    assert_transition,
    can_transition,
    is_terminal,
)


def test_all_ten_states_exist():
    """指导书 9.2 要求的 10 个状态一个不少。"""
    assert {s.value for s in OrderStatus} == {
        "CREATED", "RISK_APPROVED", "SUBMITTING", "NEW", "PARTIALLY_FILLED",
        "FILLED", "CANCELED", "REJECTED", "EXPIRED", "UNKNOWN",
    }


def test_happy_path_buy_full_fill():
    """CREATED -> RISK_APPROVED -> SUBMITTING -> NEW -> FILLED 全链路合法。"""
    path = [
        OrderStatus.CREATED, OrderStatus.RISK_APPROVED,
        OrderStatus.SUBMITTING, OrderStatus.NEW, OrderStatus.FILLED,
    ]
    for src, dst in zip(path, path[1:]):
        assert can_transition(src, dst), f"{src} -> {dst} 应合法"


def test_happy_path_partial_fill():
    path = [
        OrderStatus.RISK_APPROVED, OrderStatus.SUBMITTING, OrderStatus.NEW,
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
    ]
    for src, dst in zip(path, path[1:]):
        assert can_transition(src, dst)


def test_terminal_states_have_no_outgoing():
    for s in (
        OrderStatus.FILLED, OrderStatus.CANCELED,
        OrderStatus.REJECTED, OrderStatus.EXPIRED,
    ):
        assert is_terminal(s)
        for d in OrderStatus:
            assert not can_transition(s, d), f"终态 {s} 不应允许 {d}"


def test_unknown_resolves_to_exchange_states():
    """UNKNOWN 只能经查询恢复到交易所确认状态或回到待提交。"""
    for dst in (
        OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED,
        OrderStatus.RISK_APPROVED,
    ):
        assert can_transition(OrderStatus.UNKNOWN, dst)
    assert not can_transition(OrderStatus.UNKNOWN, OrderStatus.SUBMITTING)
    assert not can_transition(OrderStatus.UNKNOWN, OrderStatus.CREATED)


def test_illegal_transitions_raise():
    for src, dst in [
        (OrderStatus.CREATED, OrderStatus.FILLED),      # 不能跳过提交
        (OrderStatus.CREATED, OrderStatus.SUBMITTING),  # 未经风控批准
        (OrderStatus.RISK_APPROVED, OrderStatus.FILLED),
        (OrderStatus.NEW, OrderStatus.SUBMITTING),      # 不能回退
        (OrderStatus.NEW, OrderStatus.CREATED),
        (OrderStatus.FILLED, OrderStatus.CANCELED),     # 终态不可变
        (OrderStatus.REJECTED, OrderStatus.NEW),
        (OrderStatus.CANCELED, OrderStatus.FILLED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.NEW),
    ]:
        with pytest.raises(InvalidTransitionError):
            assert_transition(src, dst)


def test_rejected_can_come_from_submission_stages():
    """REJECTED 只允许出现在提交链路上(创建后未经提交不可拒绝)。"""
    assert can_transition(OrderStatus.CREATED, OrderStatus.REJECTED)
    assert can_transition(OrderStatus.RISK_APPROVED, OrderStatus.REJECTED)
    assert can_transition(OrderStatus.SUBMITTING, OrderStatus.REJECTED)


def test_assert_transition_passes_for_legal():
    assert_transition(OrderStatus.SUBMITTING, OrderStatus.UNKNOWN)
    assert_transition(OrderStatus.UNKNOWN, OrderStatus.FILLED)
