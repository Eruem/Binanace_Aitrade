"""订单对账(阶段 6,指导书 9.3/13)。

定期把本地订单意图与交易所核对:
- SUBMITTING / UNKNOWN(结果未知):查到 → 同步交易所状态;
  确认无此单 → 回退到 RISK_APPROVED(允许同一 Client Order ID 重试);
- NEW / PARTIALLY_FILLED(在途):查到 → 同步;查不到 → **危险不一致**;
- 终态:与交易所不一致(或本地认为成交/撤销但交易所查不到、
  本地认为拒绝但交易所存在)→ **危险不一致**;
- 查询本身失败(网络断线):不动任何状态,记入 errors,下轮再查
  (UNKNOWN 持续存在,风控/Manager 层面都不会盲目重发);
- 危险不一致 → Kill Switch auto_pause("任意异常时停止新开仓"),
  恢复必须人工确认(指导书 8.3/阶段 6 验收)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .interfaces import Broker
from .order_manager import OrderManager
from .order_state import OrderStatus


@dataclass
class ReconcileReport:
    """一轮对账的结果(可审计)。"""

    checked: int = 0
    synced: list[str] = field(default_factory=list)   # "cid: OLD->NEW"
    released: list[str] = field(default_factory=list)  # 确认未送达,可同 ID 重试
    divergences: list[str] = field(default_factory=list)  # 危险不一致(已停新开仓)
    errors: list[str] = field(default_factory=list)  # 查询失败(状态未动)

    @property
    def ok(self) -> bool:
        return not self.divergences and not self.errors


class OrderReconciler:
    """订单状态定期与交易所对账;发现不一致立即停止新开仓。"""

    def __init__(
        self,
        broker: Broker,
        manager: OrderManager,
        *,
        kill_switch: Any = None,  # risk.KillSwitch(避免反向依赖,鸭子类型)
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._broker = broker
        self._manager = manager
        self._kill_switch = kill_switch
        self._now = now_ms or (lambda: int(time.time() * 1000))

    async def reconcile(self) -> ReconcileReport:
        report = ReconcileReport()
        intents = self._manager.list_intents()
        for cid, entry in sorted(intents.items()):
            report.checked += 1
            status = OrderStatus(entry["status"])
            if status in (OrderStatus.CREATED, OrderStatus.RISK_APPROVED):
                continue  # 尚未提交,无须核对
            try:
                result = await self._broker.get_order(cid, symbol=entry["symbol"])
            except Exception as e:
                # 查询失败:状态一律不动(不猜测、不放宽),下轮再查
                report.errors.append(f"{cid}: {type(e).__name__}: {e}")
                continue

            if status in (OrderStatus.SUBMITTING, OrderStatus.UNKNOWN):
                if result is None:
                    self._manager.confirm_absent(cid)
                    report.released.append(f"{cid}: {status.value}->RISK_APPROVED")
                elif result.status != status:
                    self._manager.sync_from_exchange(cid, result)
                    report.synced.append(f"{cid}: {status.value}->{result.status.value}")
            elif status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
                if result is None:
                    report.divergences.append(
                        f"{cid}: 本地 {status.value} 但交易所无此单"
                    )
                elif result.status != status:
                    self._manager.sync_from_exchange(cid, result)
                    report.synced.append(f"{cid}: {status.value}->{result.status.value}")
            elif status == OrderStatus.REJECTED:
                if result is not None:
                    report.divergences.append(
                        f"{cid}: 本地 REJECTED 但交易所存在订单"
                        f"(状态 {result.status.value})"
                    )
            else:  # FILLED / CANCELED / EXPIRED 终态
                if result is None:
                    report.divergences.append(
                        f"{cid}: 本地 {status.value} 但交易所无此单"
                    )
                elif result.status != status:
                    report.divergences.append(
                        f"{cid}: 终态不一致 本地 {status.value} vs 交易所 {result.status.value}"
                    )

        if report.divergences:
            self._stop_new_opens(report)
        return report

    def _stop_new_opens(self, report: ReconcileReport) -> None:
        """危险不一致 → 停止新开仓(auto_pause;只能人工 release)。"""
        if self._kill_switch is None:
            return
        detail = "; ".join(report.divergences[:5])
        self._kill_switch.auto_pause(f"订单对账发现不一致,停止新开仓: {detail}")
