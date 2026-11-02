"""OrderManager(阶段 5,指导书 9.3)。

订单提交的业务编排,负责幂等与状态可追溯:
- Client Order ID 由 decision_id 确定性生成(同一 decision 永远同一 ID),
  只含 Binance 允许字符,长度 <= 36;
- 下单前先持久化订单意图(CREATED → RISK_APPROVED → SUBMITTING),
  再调用 Broker;提交异常 → 状态记 UNKNOWN;
- UNKNOWN / SUBMITTING 恢复:先查询 Broker(原单是否存在),
  交易所无记录才允许用同一 Client Order ID 重试;
  绝不生成新 Client Order ID 重发(指导书 9.3);
- 终态订单重复提交 → 幂等返回原结果,绝不重复记账;
- 本地意图状态与 Broker 记录不一致 → 返回 UNKNOWN + "需对账",不猜测。

安全边界:Manager 不持有任何 API Key,只调用注入的 Broker 接口。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from decimal import Decimal
from pathlib import Path
from typing import Callable

from risk.order_validator import OrderRequest

from .interfaces import Broker, BrokerError, OrderResult
from .order_state import OrderStatus, assert_transition, is_terminal

_INTENT_REQUIRED = ("intents",)


def make_client_order_id(decision_id: str) -> str:
    """由 decision_id 确定性生成稳定 Client Order ID。

    - 只保留 [A-Za-z0-9._-](Binance 允许的字符集);
    - 超长时保留前缀 + 6 位 sha1 摘要,保证唯一且 <= 36 字符;
    - 同一 decision_id 输入永远输出同一 ID(幂等的基础)。
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", decision_id)
    if len(safe) > 33:
        digest = hashlib.sha1(decision_id.encode("utf-8")).hexdigest()[:6]
        safe = f"{safe[:26]}-{digest}"
    return f"ai-{safe}"[:36]


class OrderManagerError(Exception):
    """OrderManager 误用(意图状态文件损坏、未知订单等)。"""


class OrderManager:
    """订单意图与幂等的唯一入口;Broker 之上的业务层。"""

    def __init__(
        self,
        broker: Broker,
        *,
        state_path: str | Path | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._broker = broker
        self._path = Path(state_path) if state_path else None
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._mem_state: dict | None = None  # 无 state_path 时的内存状态
        if self._path and self._path.exists():
            self._load()  # 损坏立即抛错,不留半初始化状态

    # ------------------------------------------------------------------
    # 意图持久化(原子写;损坏 = 拒绝下单,无法保证幂等就不下单)
    # ------------------------------------------------------------------
    def _blank(self) -> dict:
        return {"intents": {}}

    def _load(self) -> dict:
        if self._path is None:
            # 内存模式:同一 dict 对象贯穿整个生命周期
            if self._mem_state is None:
                self._mem_state = self._blank()
            return self._mem_state
        if not self._path.exists():
            return self._blank()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or any(k not in raw for k in _INTENT_REQUIRED):
                raise ValueError("意图文件结构不完整")
            return raw
        except (OSError, ValueError, TypeError) as e:
            raise OrderManagerError(
                f"订单意图文件损坏,拒绝下单(无法保证幂等): {e}"
            ) from e

    def _save(self, data: dict) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, self._path)

    # ------------------------------------------------------------------
    # 意图状态推进(全部经状态机校验)
    # ------------------------------------------------------------------
    def _append(
        self,
        data: dict,
        client_order_id: str,
        status: OrderStatus,
        *,
        error: str | None = None,
        exchange_order_id: str | None = None,
        record_status: bool = True,
    ) -> dict:
        """推进意图状态并持久化。record_status=False 时不覆盖已有 error。"""
        entry = data["intents"][client_order_id]
        cur = OrderStatus(entry["status"])
        if cur != status:
            assert_transition(cur, status)
            entry["history"].append(status.value)
            entry["status"] = status.value
        entry["updated_at"] = self._now()
        if record_status:
            entry["error"] = error
        if exchange_order_id is not None:
            entry["exchange_order_id"] = exchange_order_id
        self._save(data)
        return entry

    def _unknown_result(
        self, request: OrderRequest, client_order_id: str, error: str, created_at: int
    ) -> OrderResult:
        now = self._now()
        return OrderResult(
            client_order_id=client_order_id,
            exchange_order_id=None,
            decision_id=request.decision_id,
            symbol=request.symbol,
            side=request.side,  # type: ignore[arg-type]
            status=OrderStatus.UNKNOWN,
            amount_usdt=request.amount_usdt,
            requested_qty=Decimal("0"),
            filled_qty=Decimal("0"),
            avg_price=None,
            fee=Decimal("0"),
            fee_currency="USDT",
            created_at=created_at,
            updated_at=now,
            error=error,
        )

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def client_order_id(self, decision_id: str) -> str:
        return make_client_order_id(decision_id)

    def intent(self, client_order_id: str) -> dict | None:
        """查询本地订单意图(测试/排错用)。"""
        data = self._load()
        entry = data["intents"].get(client_order_id)
        return dict(entry) if entry else None

    def list_intents(self) -> dict[str, dict]:
        """全部订单意图快照(阶段 6 对账用)。"""
        data = self._load()
        return {cid: dict(e) for cid, e in data["intents"].items()}

    def _release_absent(self, data: dict, client_order_id: str) -> None:
        """查询确认交易所无此单:回退到已批准,允许同一 Client Order ID 重试。"""
        status = OrderStatus(data["intents"][client_order_id]["status"])
        if status in (OrderStatus.SUBMITTING, OrderStatus.UNKNOWN):
            self._append(
                data, client_order_id, OrderStatus.RISK_APPROVED,
                error="查询确认交易所无此单,允许同 ID 重试",
            )

    def confirm_absent(self, client_order_id: str) -> None:
        """对账确认交易所无此单:意图回退到 RISK_APPROVED(可同 ID 重试)。"""
        data = self._load()
        if client_order_id not in data["intents"]:
            raise OrderManagerError(f"未知订单意图: {client_order_id}")
        self._release_absent(data, client_order_id)

    def sync_from_exchange(self, client_order_id: str, result: OrderResult) -> dict:
        """用交易所返回的最新状态同步本地意图(经状态机校验)。"""
        data = self._load()
        entry = data["intents"].get(client_order_id)
        if entry is None:
            raise OrderManagerError(f"未知订单意图: {client_order_id}")
        if OrderStatus(entry["status"]) != result.status:
            self._append(
                data, client_order_id, result.status,
                error=result.error,
                exchange_order_id=result.exchange_order_id,
            )
        elif result.exchange_order_id and not entry.get("exchange_order_id"):
            entry["exchange_order_id"] = result.exchange_order_id
            self._save(data)
        return dict(data["intents"][client_order_id])

    async def submit(self, request: OrderRequest) -> OrderResult:
        """提交订单(幂等;先持久化意图,再调 Broker)。"""
        if request.side not in ("BUY", "SELL"):
            raise OrderManagerError(f"非法方向 {request.side!r}")
        cid = make_client_order_id(request.decision_id)
        data = self._load()
        now = self._now()
        entry = data["intents"].get(cid)

        if entry is not None:
            status = OrderStatus(entry["status"])
            if status in (OrderStatus.SUBMITTING, OrderStatus.UNKNOWN):
                # 未确认原订单结果:先查询,绝不直接换 ID 重发(9.3)
                existing = await self._broker.get_order(
                    cid, symbol=entry["symbol"]
                )
                if existing is not None:
                    if existing.status != status:
                        self._append(
                            data,
                            cid,
                            existing.status,
                            error=existing.error,
                            exchange_order_id=existing.exchange_order_id,
                        )
                    return existing
                # 交易所确认无此单 → 回退到已批准,可用同一 ID 重试
                self._release_absent(data, cid)
                entry = data["intents"][cid]
            elif status not in (OrderStatus.CREATED, OrderStatus.RISK_APPROVED):
                # 已有终态/在途订单:幂等返回,绝不重复记账
                existing = await self._broker.get_order(
                    cid, symbol=entry["symbol"]
                )
                if existing is not None:
                    return existing
                # 本地有记录但 Broker 无记录:状态不一致,拒绝对账前重发
                return self._unknown_result(
                    request, cid,
                    f"本地意图状态 {status.value} 但执行层无记录,需人工对账",
                    entry["created_at"],
                )
            # CREATED / RISK_APPROVED:直接进入(重新)提交流程
            if entry["status"] == OrderStatus.CREATED.value:
                # 防御:CREATED 不落盘,但若出现则先推进到 RISK_APPROVED
                self._append(data, cid, OrderStatus.RISK_APPROVED)

        if entry is None:
            data["intents"][cid] = {
                "client_order_id": cid,
                "decision_id": request.decision_id,
                "symbol": request.symbol,
                "side": request.side,
                "amount_usdt": str(request.amount_usdt),
                "created_at": now,
                "updated_at": now,
                "status": OrderStatus.CREATED.value,
                "history": [OrderStatus.CREATED.value],
                "error": None,
                "exchange_order_id": None,
            }
            # 风控放行后进入 Manager:立即标记 RISK_APPROVED
            self._append(data, cid, OrderStatus.RISK_APPROVED)
            entry = data["intents"][cid]

        # 关键顺序:先持久化 SUBMITTING(意图),再调用 Broker
        self._append(data, cid, OrderStatus.SUBMITTING)
        try:
            result = await self._broker.submit_order(request, client_order_id=cid)
        except Exception as e:
            self._append(
                data, cid, OrderStatus.UNKNOWN,
                error=f"{type(e).__name__}: {e}",
            )
            return self._unknown_result(
                request, cid, f"{type(e).__name__}: {e}", entry["created_at"]
            )

        self._append(
            data, cid, result.status,
            error=result.error,
            exchange_order_id=result.exchange_order_id,
        )
        return result

    async def recover(self, client_order_id: str) -> OrderResult | None:
        """UNKNOWN/SUBMITTING 恢复:查询 Broker 并同步本地意图。

        返回 Broker 的最新结果;交易所无记录时把意图回退到 RISK_APPROVED
        (允许同一 Client Order ID 重试),返回 None。
        """
        data = self._load()
        entry = data["intents"].get(client_order_id)
        if entry is None:
            raise OrderManagerError(f"未知订单意图: {client_order_id}")
        existing = await self._broker.get_order(
            client_order_id, symbol=entry["symbol"]
        )
        if existing is None:
            self._release_absent(data, client_order_id)
            return None
        if existing.status != OrderStatus(entry["status"]):
            self._append(
                data, client_order_id, existing.status,
                error=existing.error,
                exchange_order_id=existing.exchange_order_id,
            )
        return existing
