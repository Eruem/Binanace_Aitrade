"""Telegram 通知(阶段 7,指导书 12)。

安全边界(不可跨越):
- **仅发送通知**:只有 sendMessage 一个出站动作;
- **绝不执行**聊天文本中的任何指令(本模块没有入站处理、没有
  命令解析、没有轮询/长连接;测试断言模块无任何交易接口);
- Token 只经构造参数从配置注入;日志输出经 RedactionFilter 脱敏;
- 发送失败只记警告,绝不抛出、绝不影响交易主流程(通知是旁路)。
"""

from __future__ import annotations

from typing import Any

import httpx

from monitoring.logger import get_logger

_log = get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """只发不收的通知器。未配置 token/chat_id 时是 no-op。"""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 5.0,
        proxy_url: str | None = None,
    ):
        self._token = bot_token or ""
        self._chat_id = chat_id or ""
        # api.telegram.org 在本网络需经代理。httpx 默认会经 trust_env 读
        # 系统代理(Windows 注册表回退),但显式注入 EXCHANGE_PROXY_URL
        # 使行为确定、不依赖本机注册表状态(与交易所流量同一模式)
        self._client = client or httpx.Client(timeout=timeout, proxy=proxy_url)

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    def send(self, text: str) -> bool:
        """发送一条通知;失败/未启用返回 False,永不抛出。"""
        if not self.enabled:
            return False
        try:
            resp = self._client.post(
                _TELEGRAM_API.format(token=self._token),
                json={"chat_id": self._chat_id, "text": text[:4000]},
            )
            if resp.status_code >= 400:
                _log.warning(
                    "telegram 通知发送失败(忽略,不影响交易)",
                    extra={"result_status": "error", "error_code": str(resp.status_code)},
                )
                return False
            return True
        except Exception as e:  # 网络错误等:通知是旁路,绝不影响主流程
            _log.warning(
                "telegram 通知异常(忽略,不影响交易): %s",
                type(e).__name__,
                extra={"result_status": "error"},
            )
            return False

    # ------------------------------------------------------------------ 摘要

    def send_cycle_summary(
        self,
        *,
        cycle_id: str,
        symbol: str,
        final_status: str,
        decision_id: str | None = None,
        action: str | None = None,
        amount_usdt: Any = None,
        risk_verdict: str | None = None,
        note: str | None = None,
    ) -> bool:
        """一个 cycle 的标准摘要文案(不含任何密钥)。"""
        lines = [
            f"[cycle] {cycle_id}",
            f"symbol: {symbol}",
            f"status: {final_status}",
        ]
        if decision_id:
            lines.append(f"decision: {decision_id}")
        if action:
            lines.append(f"action: {action}")
        if amount_usdt is not None:
            lines.append(f"amount: {amount_usdt} USDT")
        if risk_verdict:
            lines.append(f"risk: {risk_verdict}")
        if note:
            lines.append(f"note: {note}")
        return self.send("\n".join(lines))


def build_notifier(
    bot_token: str, chat_id: str, proxy_url: str | None = None
) -> TelegramNotifier:
    """按配置构建通知器(空配置 = no-op;proxy_url 用于被阻断的网络环境)。"""
    return TelegramNotifier(bot_token, chat_id, proxy_url=proxy_url)
