"""Kill Switch(阶段 4,指导书 8.3)。

语义:
- 开关只禁止"新开仓"(BUY);平仓/减仓不受限——平仓不等于停止新开仓;
- 手动紧急停止:engage(source="manual");
- 系统异常自动暂停:auto_pause(source="auto");
- 恢复必须人工操作:release() 只接受 source="manual",auto 状态不可自动恢复;
- 状态持久化到本地 JSON,重启后仍生效(宁可误停,不可漏停);
- LLM 无任何接口可以操作本开关。

撤单(cancel open orders)是独立操作,需单独配置(阶段 5/6),不在本模块。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path


class KillSwitchError(Exception):
    """Kill Switch 误用(如尝试自动恢复)。"""


@dataclass(frozen=True)
class KillSwitchState:
    engaged: bool
    reason: str
    source: str  # manual | auto
    engaged_at: int
    released_at: int | None = None


class KillSwitch:
    def __init__(self, state_path: str | Path | None = None) -> None:
        self._path = Path(state_path) if state_path else None
        self._lock = threading.Lock()
        self._state = KillSwitchState(engaged=False, reason="", source="", engaged_at=0)
        if self._path and self._path.exists():
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
            self._state = KillSwitchState(
                engaged=bool(data.get("engaged", False)),
                reason=str(data.get("reason", "")),
                source=str(data.get("source", "")),
                engaged_at=int(data.get("engaged_at", 0)),
                released_at=data.get("released_at"),
            )
        except (OSError, ValueError):
            # 状态文件损坏时按"已触发"处理(安全默认:宁可停)
            self._state = KillSwitchState(
                engaged=True,
                reason="kill switch 状态文件损坏,按已触发处理",
                source="auto",
                engaged_at=int(time.time() * 1000),
            )

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._state.__dict__, ensure_ascii=False), encoding="utf-8"
        )

    @property
    def state(self) -> KillSwitchState:
        return self._state

    @property
    def is_engaged(self) -> bool:
        return self._state.engaged

    def engage(self, reason: str, *, source: str = "manual") -> KillSwitchState:
        """触发开关。幂等:已触发时只更新记录,不产生副作用。"""
        if source not in ("manual", "auto"):
            raise KillSwitchError(f"未知触发来源: {source!r}")
        with self._lock:
            now = int(time.time() * 1000)
            if self._state.engaged:
                return self._state
            self._state = KillSwitchState(
                engaged=True, reason=reason, source=source, engaged_at=now
            )
            self._save()
            return self._state

    def auto_pause(self, error: str) -> KillSwitchState:
        """系统异常自动暂停新开仓。"""
        return self.engage(f"系统异常自动暂停: {error}", source="auto")

    def release(self, reason: str) -> KillSwitchState:
        """人工恢复。auto 触发的状态也必须经此人工步骤恢复。"""
        with self._lock:
            if not self._state.engaged:
                return self._state
            now = int(time.time() * 1000)
            self._state = KillSwitchState(
                engaged=False,
                reason=reason,
                source="manual",
                engaged_at=self._state.engaged_at,
                released_at=now,
            )
            self._save()
            return self._state
