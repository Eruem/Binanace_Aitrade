"""阶段 5:OrderManager 测试(指导书 9.3 幂等与恢复)。

全部离线:注入 PaperBroker 或假 Broker,不发任何网络请求。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from exchange.interfaces import Broker, OrderResult
from exchange.order_manager import (
    OrderManager,
    OrderManagerError,
    make_client_order_id,
)
from exchange.order_state import OrderStatus
from exchange.paper_broker import PaperBroker
from risk.order_validator import OrderRequest

D = Decimal
SYMBOL = "BTC/USDT"
REF = D("50000")


def make_request(decision_id: str = "cycle-m1", amount: str = "500.5") -> OrderRequest:
    return OrderRequest(
        decision_id=decision_id, symbol=SYMBOL, side="BUY", amount_usdt=D(amount)
    )


def make_broker(tmp_path, **over) -> PaperBroker:
    kw = dict(
        price_provider=lambda s: REF,
        state_path=tmp_path / "paper.json",
        fee_rate="0.001",
        slippage_rate="0.001",
    )
    kw.update(over)
    return PaperBroker(**kw)


def make_manager(broker, tmp_path, **over) -> OrderManager:
    kw = dict(state_path=tmp_path / "intents.json")
    kw.update(over)
    return OrderManager(broker, **kw)


def intents_json(tmp_path) -> dict:
    return json.loads((tmp_path / "intents.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Client Order ID
# ---------------------------------------------------------------------------

def test_client_order_id_stable_and_safe():
    """同一 decision_id 永远生成同一 ID;只含允许字符且 <= 36。"""
    a = make_client_order_id("cycle-r-ok")
    b = make_client_order_id("cycle-r-ok")
    assert a == b
    assert len(a) <= 36
    assert all(c.isalnum() or c in "._-" for c in a)


def test_client_order_id_unique_per_decision():
    assert make_client_order_id("cycle-1") != make_client_order_id("cycle-2")
    # 超长 decision_id:截断 + 摘要,仍唯一
    long1 = "cycle-" + "x" * 80
    long2 = "cycle-" + "x" * 80 + "y"
    assert make_client_order_id(long1) != make_client_order_id(long2)
    assert len(make_client_order_id(long1)) <= 36
    # 特殊字符被清洗
    assert "/" not in make_client_order_id("cycle/坏 decision")


# ---------------------------------------------------------------------------
# 提交与幂等
# ---------------------------------------------------------------------------

async def test_submit_success_persists_intent_before_order(tmp_path):
    """意图先持久化(CREATED→RISK_APPROVED→SUBMITTING),再有订单结果。"""
    broker = make_broker(tmp_path)
    manager = make_manager(broker, tmp_path)
    result = await manager.submit(make_request())
    assert result.status == OrderStatus.FILLED
    cid = result.client_order_id
    entry = intents_json(tmp_path)["intents"][cid]
    assert entry["history"] == [
        "CREATED", "RISK_APPROVED", "SUBMITTING", "FILLED",
    ]
    assert entry["exchange_order_id"] == "paper-1"


async def test_submit_intent_is_on_disk_before_broker_call(tmp_path):
    """Broker 被调用时,意图文件里必须已经是 SUBMITTING(9.3:先持久化)。"""
    seen: dict[str, str] = {}

    class SpyBroker(Broker):
        name = "spy"

        async def submit_order(self, request, *, client_order_id):
            raw = json.loads((tmp_path / "intents.json").read_text(encoding="utf-8"))
            seen["status"] = raw["intents"][client_order_id]["status"]
            return await inner.submit_order(request, client_order_id=client_order_id)

        async def get_order(self, client_order_id, *, symbol=None):
            return await inner.get_order(client_order_id, symbol=symbol)

        async def cancel_order(self, client_order_id, *, symbol=None):
            return await inner.cancel_order(client_order_id, symbol=symbol)

        async def get_account(self):
            return await inner.get_account()

    inner = make_broker(tmp_path)
    manager = make_manager(SpyBroker(), tmp_path)
    await manager.submit(make_request())
    assert seen["status"] == "SUBMITTING"  # 意图已落盘,提交尚未返回


async def test_duplicate_submit_returns_same_order_no_double_book(tmp_path):
    """验收:重复提交不会重复记账。"""
    broker = make_broker(tmp_path)
    manager = make_manager(broker, tmp_path)
    first = await manager.submit(make_request("cycle-dup"))
    second = await manager.submit(make_request("cycle-dup"))  # 同 decision 重试
    assert second.client_order_id == first.client_order_id
    assert second.exchange_order_id == first.exchange_order_id
    assert second.status == OrderStatus.FILLED
    assert len(broker.trades()) == 1  # 只有一笔成交
    assert len(intents_json(tmp_path)["intents"]) == 1
    account = await broker.get_account()
    assert account.available_usdt == D("9498.9995")  # 只扣一次款


def memory_manager() -> OrderManager:
    """内存模式 Manager(无持久化文件;仅测试误用路径)。"""
    return OrderManager(PaperBroker(price_provider=lambda s: REF))


async def test_illegal_side_raises():
    manager = memory_manager()
    with pytest.raises(OrderManagerError):
        await manager.submit(
            OrderRequest(
                decision_id="cycle-bad", symbol=SYMBOL, side="SHORT",
                amount_usdt=D("10"),
            )
        )


# ---------------------------------------------------------------------------
# UNKNOWN 与恢复(指导书 9.3)
# ---------------------------------------------------------------------------

class FlakyBroker(Broker):
    """第一次提交抛异常(模拟超时/断线),之后转发给真实 Broker。"""

    name = "flaky"

    def __init__(self, inner: Broker, *, reaches_exchange: bool) -> None:
        self._inner = inner
        self._fail_next = True
        self.reaches_exchange = reaches_exchange  # 请求是否实际到达了交易所

    async def submit_order(self, request, *, client_order_id):
        if self._fail_next:
            self._fail_next = False
            if self.reaches_exchange:
                await self._inner.submit_order(
                    request, client_order_id=client_order_id
                )  # 交易所已受理,但响应丢失
            raise TimeoutError("模拟提交超时")
        return await self._inner.submit_order(request, client_order_id=client_order_id)

    async def get_order(self, client_order_id, *, symbol=None):
        return await self._inner.get_order(client_order_id, symbol=symbol)

    async def cancel_order(self, client_order_id, *, symbol=None):
        return await self._inner.cancel_order(client_order_id, symbol=symbol)

    async def get_account(self):
        return await self._inner.get_account()


async def test_timeout_marks_unknown_and_never_new_id(tmp_path):
    """超时 → UNKNOWN;恢复时用同一 client_order_id,绝不生成新 ID。"""
    inner = make_broker(tmp_path)
    flaky = FlakyBroker(inner, reaches_exchange=False)
    manager = make_manager(flaky, tmp_path)

    first = await manager.submit(make_request("cycle-to"))
    assert first.status == OrderStatus.UNKNOWN
    assert first.error and "TimeoutError" in first.error
    cid = first.client_order_id
    entry = intents_json(tmp_path)["intents"][cid]
    assert entry["history"][-1] == "UNKNOWN"

    # 恢复:交易所无此单 → 释放回待提交,同一 ID 重试成功
    assert await manager.recover(cid) is None
    assert intents_json(tmp_path)["intents"][cid]["status"] == "RISK_APPROVED"
    second = await manager.submit(make_request("cycle-to"))
    assert second.client_order_id == cid  # 同一 ID
    assert second.status == OrderStatus.FILLED
    assert len(inner.trades()) == 1


async def test_timeout_but_order_reached_exchange(tmp_path):
    """请求实际已受理但响应丢失:恢复时查到原单,不再重发。"""
    inner = make_broker(tmp_path)
    flaky = FlakyBroker(inner, reaches_exchange=True)
    manager = make_manager(flaky, tmp_path)

    first = await manager.submit(make_request("cycle-to2"))
    assert first.status == OrderStatus.UNKNOWN

    # 再次提交:Manager 先查询,发现原单已 FILLED → 幂等返回,不重复下单
    second = await manager.submit(make_request("cycle-to2"))
    assert second.status == OrderStatus.FILLED
    assert len(inner.trades()) == 1  # 交易所只成交一次


async def test_recover_unknown_finds_filled_order(tmp_path):
    """recover():UNKNOWN 后查询到 FILLED,意图同步为 FILLED。"""
    inner = make_broker(tmp_path)
    flaky = FlakyBroker(inner, reaches_exchange=True)
    manager = make_manager(flaky, tmp_path)
    first = await manager.submit(make_request("cycle-rc"))
    assert first.status == OrderStatus.UNKNOWN

    recovered = await manager.recover(first.client_order_id)
    assert recovered is not None and recovered.status == OrderStatus.FILLED
    entry = intents_json(tmp_path)["intents"][first.client_order_id]
    assert entry["status"] == "FILLED"
    assert entry["history"][-1] == "FILLED"  # UNKNOWN -> FILLED 合法流转


async def test_resubmit_after_unknown_auto_recovers_when_no_order(tmp_path):
    """UNKNOWN 后直接再提交(不显式 recover):Manager 内部先查询,
    交易所无单才用同一 ID 重试。"""
    inner = make_broker(tmp_path)
    flaky = FlakyBroker(inner, reaches_exchange=False)
    manager = make_manager(flaky, tmp_path)
    first = await manager.submit(make_request("cycle-rc2"))
    assert first.status == OrderStatus.UNKNOWN
    second = await manager.submit(make_request("cycle-rc2"))
    assert second.status == OrderStatus.FILLED
    assert second.client_order_id == first.client_order_id


async def test_recover_unknown_intent_raises():
    manager = memory_manager()
    with pytest.raises(OrderManagerError):
        await manager.recover("ai-not-exist")


async def test_corrupt_intent_file_refuses_orders(tmp_path):
    """意图文件损坏 → 拒绝下单(无法保证幂等就不下单)。"""
    (tmp_path / "intents.json").write_text("not-json{{", encoding="utf-8")
    with pytest.raises(OrderManagerError):
        make_manager(make_broker(tmp_path), tmp_path)
