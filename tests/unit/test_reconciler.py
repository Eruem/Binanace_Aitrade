"""阶段 6:订单对账测试(指导书 9.3/13/14 #12/#14/#15)。

重启后能对账恢复;网络超时不盲目重发;任意异常时停止新开仓。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from agents.schemas import Action, StrategyName, TradingDecision
from config.loader import RiskParams
from exchange.interfaces import Broker, OrderResult
from exchange.order_manager import OrderManager, make_client_order_id
from exchange.order_reconciler import OrderReconciler
from exchange.order_state import OrderStatus
from risk.engine import AccountSnapshot, RiskEngine
from risk.kill_switch import KillSwitch
from risk.order_validator import OrderRequest

D = Decimal
NOW = 1_793_145_600_000
SYMBOL = "BTC/USDT"
DECISION_ID = "cycle-rc-0001"
CID = make_client_order_id(DECISION_ID)


def make_request() -> OrderRequest:
    return OrderRequest(
        decision_id=DECISION_ID, symbol=SYMBOL, side="BUY", amount_usdt=D("500")
    )


def result(status: OrderStatus, **over) -> OrderResult:
    base = dict(
        client_order_id=CID,
        exchange_order_id="42",
        decision_id=DECISION_ID,
        symbol=SYMBOL,
        side="BUY",
        status=status,
        amount_usdt=D("500"),
        requested_qty=D("0.01"),
        filled_qty=D("0") if status != OrderStatus.FILLED else D("0.01"),
        avg_price=None if status != OrderStatus.FILLED else D("50000"),
        fee=D("0.5"),
        fee_currency="USDT",
        created_at=NOW - 1000,
        updated_at=NOW,
        error=None,
    )
    base.update(over)
    return OrderResult(**base)


class StubBroker(Broker):
    """可编排的假 Broker:预设 get_order 序列/异常。"""

    name = "stub"

    def __init__(self) -> None:
        self.next_get: list = []  # 依次弹出:OrderResult | None | Exception
        self.get_calls: list[str] = []

    async def submit_order(self, request, *, client_order_id):
        raise NotImplementedError("对账测试不提交新单")

    async def get_order(self, client_order_id, *, symbol=None):
        self.get_calls.append(client_order_id)
        if self.next_get:
            item = self.next_get.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return None

    async def cancel_order(self, client_order_id, *, symbol=None):
        raise NotImplementedError

    async def get_account(self):
        raise NotImplementedError


def seed_intent(
    path: Path,
    status: OrderStatus,
    *,
    history: list[str] | None = None,
    cid: str = CID,
) -> None:
    """直接写入意图文件,构造任意本地状态(模拟崩溃现场)。"""
    hist = history or [OrderStatus.CREATED.value, OrderStatus.RISK_APPROVED.value]
    if not hist or hist[-1] != status.value:
        hist = [*hist, status.value]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "intents": {
                    cid: {
                        "client_order_id": cid,
                        "decision_id": DECISION_ID,
                        "symbol": SYMBOL,
                        "side": "BUY",
                        "amount_usdt": "500",
                        "created_at": NOW - 5000,
                        "updated_at": NOW - 1000,
                        "status": status.value,
                        "history": hist,
                        "error": None,
                        "exchange_order_id": None,
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def make_pair(tmp_path, broker: StubBroker, **over):
    manager = OrderManager(
        broker, state_path=tmp_path / "intents.json", now_ms=lambda: NOW, **over
    )
    return manager, OrderReconciler(broker, manager, now_ms=lambda: NOW)


# ----------------------------------------------------------------------
# 未提交的意图:跳过
# ----------------------------------------------------------------------
async def test_created_and_approved_skipped(tmp_path):
    broker = StubBroker()
    seed_intent(tmp_path / "intents.json", OrderStatus.CREATED)
    manager, rec = make_pair(tmp_path, broker)
    report = await rec.reconcile()
    assert report.checked == 1
    assert broker.get_calls == []  # 尚未提交,不去交易所


# ----------------------------------------------------------------------
# SUBMITTING / UNKNOWN:查到 → 同步;查无 → 释放可同 ID 重试
# ----------------------------------------------------------------------
async def test_unknown_synced_when_found_on_exchange(tmp_path):
    """超时标记 UNKNOWN 后查到订单 → 同步交易所状态(#12 盲目重发防线)。"""
    broker = StubBroker()
    seed_intent(
        tmp_path / "intents.json",
        OrderStatus.UNKNOWN,
        history=[
            OrderStatus.CREATED.value,
            OrderStatus.RISK_APPROVED.value,
            OrderStatus.SUBMITTING.value,
        ],
    )
    broker.next_get = [result(OrderStatus.FILLED)]
    manager, rec = make_pair(tmp_path, broker)
    report = await rec.reconcile()
    assert f"{CID}: UNKNOWN->FILLED" in report.synced
    assert manager.intent(CID)["status"] == OrderStatus.FILLED.value
    assert report.divergences == []


async def test_unknown_absent_released_for_same_id_retry(tmp_path):
    """确认交易所无此单 → 回退 RISK_APPROVED,允许同一 Client Order ID 重试。"""
    broker = StubBroker()
    seed_intent(
        tmp_path / "intents.json",
        OrderStatus.UNKNOWN,
        history=[
            OrderStatus.CREATED.value,
            OrderStatus.RISK_APPROVED.value,
            OrderStatus.SUBMITTING.value,
        ],
    )
    broker.next_get = [None]
    manager, rec = make_pair(tmp_path, broker)
    report = await rec.reconcile()
    assert report.released and CID in report.released[0]
    entry = manager.intent(CID)
    assert entry["status"] == OrderStatus.RISK_APPROVED.value
    # 释放后同一 ID 重试不会换 ID(幂等基础)
    assert manager.client_order_id(DECISION_ID) == CID


async def test_submitting_synced_to_new(tmp_path):
    broker = StubBroker()
    seed_intent(tmp_path / "intents.json", OrderStatus.SUBMITTING)
    broker.next_get = [result(OrderStatus.NEW)]
    manager, rec = make_pair(tmp_path, broker)
    report = await rec.reconcile()
    assert f"{CID}: SUBMITTING->NEW" in report.synced
    assert manager.intent(CID)["status"] == OrderStatus.NEW.value


# ----------------------------------------------------------------------
# 在途订单 NEW / PARTIALLY_FILLED:查无 → 危险不一致 → 停新开仓
# ----------------------------------------------------------------------
async def test_new_but_absent_is_divergence_and_pauses(tmp_path):
    """本地在途但交易所无单 → 危险不一致 → Kill Switch 停止新开仓。"""
    broker = StubBroker()
    seed_intent(
        tmp_path / "intents.json",
        OrderStatus.NEW,
        history=[
            OrderStatus.CREATED.value,
            OrderStatus.RISK_APPROVED.value,
            OrderStatus.SUBMITTING.value,
        ],
    )
    broker.next_get = [None]
    ks = KillSwitch()
    manager, _ = make_pair(tmp_path, broker)
    rec = OrderReconciler(broker, manager, kill_switch=ks, now_ms=lambda: NOW)
    report = await rec.reconcile()
    assert report.divergences and "无此单" in report.divergences[0]
    assert ks.is_engaged and ks.state.source == "auto"
    assert "停止新开仓" in ks.state.reason


async def test_partial_filled_synced_to_filled(tmp_path):
    broker = StubBroker()
    seed_intent(tmp_path / "intents.json", OrderStatus.PARTIALLY_FILLED)
    broker.next_get = [result(OrderStatus.FILLED)]
    manager, rec = make_pair(tmp_path, broker)
    report = await rec.reconcile()
    assert f"{CID}: PARTIALLY_FILLED->FILLED" in report.synced


# ----------------------------------------------------------------------
# 终态 / REJECTED 与交易所不一致 → 危险不一致
# ----------------------------------------------------------------------
async def test_terminal_absent_is_divergence(tmp_path):
    broker = StubBroker()
    seed_intent(tmp_path / "intents.json", OrderStatus.FILLED)
    broker.next_get = [None]
    manager, rec = make_pair(tmp_path, broker)
    report = await rec.reconcile()
    assert report.divergences


async def test_terminal_status_mismatch_is_divergence(tmp_path):
    broker = StubBroker()
    seed_intent(tmp_path / "intents.json", OrderStatus.FILLED)
    broker.next_get = [result(OrderStatus.CANCELED)]
    manager, rec = make_pair(tmp_path, broker)
    report = await rec.reconcile()
    assert report.divergences and "终态不一致" in report.divergences[0]


async def test_rejected_but_exists_is_divergence(tmp_path):
    """本地认为 REJECTED 但交易所存在订单 → 最危险的不一致(可能已成交)。"""
    broker = StubBroker()
    seed_intent(tmp_path / "intents.json", OrderStatus.REJECTED)
    broker.next_get = [result(OrderStatus.FILLED)]
    manager, rec = make_pair(tmp_path, broker)
    report = await rec.reconcile()
    assert report.divergences and "本地 REJECTED" in report.divergences[0]


# ----------------------------------------------------------------------
# 查询失败:状态一律不动(#15 网络超时不盲目重发)
# ----------------------------------------------------------------------
async def test_query_failure_leaves_state_untouched(tmp_path):
    class Boom(Exception):
        pass

    broker = StubBroker()
    seed_intent(tmp_path / "intents.json", OrderStatus.UNKNOWN)
    broker.next_get = [Boom("network down")]
    manager, rec = make_pair(tmp_path, broker)
    report = await rec.reconcile()
    assert report.errors and "Boom" in report.errors[0]
    assert manager.intent(CID)["status"] == OrderStatus.UNKNOWN.value  # 未动
    assert report.released == [] and report.synced == []
    assert report.ok is False


# ----------------------------------------------------------------------
# 崩溃恢复:重启后新实例从磁盘状态对账(#15)
# ----------------------------------------------------------------------
async def test_crash_restart_recovery(tmp_path):
    """SUBMITTING 落盘后进程崩溃;新进程对账 → 同步交易所 FILLED。"""
    state = tmp_path / "intents.json"
    broker = StubBroker()
    # 第一进程:正常走到 SUBMITTING 后"崩溃"(Broker 抛异常 → UNKNOWN)
    seed_intent(state, OrderStatus.UNKNOWN)
    # 重启:全新 Manager + Reconciler(仅从磁盘恢复)
    manager2, rec2 = make_pair(tmp_path, broker)
    broker.next_get = [result(OrderStatus.FILLED)]
    report = await rec2.reconcile()
    assert report.synced
    assert manager2.intent(CID)["status"] == OrderStatus.FILLED.value
    # 恢复后重复提交:幂等返回已成交结果,绝不重复下单
    submitted: list[str] = []

    class QueryOnlyBroker(StubBroker):
        async def submit_order(self, request, *, client_order_id):
            submitted.append(client_order_id)
            return result(OrderStatus.FILLED)

    broker2 = QueryOnlyBroker()
    broker2.next_get = [result(OrderStatus.FILLED)]
    manager3 = OrderManager(broker2, state_path=state, now_ms=lambda: NOW)
    final = await manager3.submit(make_request())
    assert final.status == OrderStatus.FILLED
    assert submitted == []  # 没有产生新的下单调用


# ----------------------------------------------------------------------
# 停新开仓联动风控:Kill Switch 触发后 BUY 被拒(SELL 不受限)
# ----------------------------------------------------------------------
async def test_divergence_blocks_new_buys_via_risk(tmp_path):
    broker = StubBroker()
    seed_intent(tmp_path / "intents.json", OrderStatus.NEW)
    broker.next_get = [None]
    ks = KillSwitch()
    manager, _ = make_pair(tmp_path, broker)
    rec = OrderReconciler(broker, manager, kill_switch=ks, now_ms=lambda: NOW)
    await rec.reconcile()
    assert ks.is_engaged

    engine = RiskEngine(
        params=RiskParams(),
        allowed_symbols=[SYMBOL],
        kill_switch=ks,
    )
    decision = TradingDecision(
        decision_id="cycle-next-01",
        symbol=SYMBOL,
        action=Action.BUY,
        strategy=StrategyName.DCA,
        amount_usdt=D("50"),
        confidence=0.5,
        reason="test",
        created_at=NOW,
        expires_at=NOW + 3_600_000,
    )
    account = AccountSnapshot(
        equity_usdt=D("10000"),
        available_usdt=D("10000"),
        fetched_at=NOW,
    )
    verdict = engine.evaluate(decision, account)
    assert verdict.verdict == "REJECTED"
    assert any("Kill Switch" in r for r in verdict.reasons)


async def test_reconcile_without_kill_switch_still_reports(tmp_path):
    """未注入 Kill Switch 时仍产出对账报告(由调用方决定处置)。"""
    broker = StubBroker()
    seed_intent(tmp_path / "intents.json", OrderStatus.NEW)
    broker.next_get = [None]
    manager, rec = make_pair(tmp_path, broker)
    report = await rec.reconcile()
    assert report.divergences
