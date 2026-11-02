"""监控层测试(阶段 7,指导书 12):日志脱敏、指标、Telegram 仅发送。"""

from __future__ import annotations

import io
import json
import logging

import httpx
import pytest

from monitoring.logger import (
    JsonFormatter,
    RedactionFilter,
    get_logger,
    log_event,
    redact,
)
from monitoring.metrics import MetricsCollector
from monitoring.telegram import TelegramNotifier

DUMMY_KEY = "sk-test-dummy-key-ABCDEF-98765"
TG_TOKEN = "123456789:AAHdummytokenabcdefghijklmnopqrstuv"


# ------------------------------------------------------------------ 脱敏


def test_redact_api_key():
    assert DUMMY_KEY not in redact(f"key is {DUMMY_KEY} end")


def test_redact_bearer():
    assert "abcdef123" not in redact("Authorization: Bearer abcdef123")


def test_redact_telegram_token():
    assert TG_TOKEN not in redact(f"token={TG_TOKEN}")


def test_redact_db_password_in_url():
    out = redact("postgresql+psycopg://postgres:secretpw@host:5432/db")
    assert "secretpw" not in out
    assert "****" in out


def test_redact_key_value_pairs():
    out = redact('{"api_key": "mykey123", "api_secret": "shhh"}')
    assert "mykey123" not in out
    assert "shhh" not in out


def test_redaction_filter_applies_to_record():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, f"leak {DUMMY_KEY}", (), None)
    f = RedactionFilter()
    assert f.filter(record) is True
    assert DUMMY_KEY not in record.getMessage()


# ------------------------------------------------------------------ JSON 格式


def test_json_formatter_includes_standard_fields():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("std-fields-test")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    log_event(logger, logging.INFO, "hello", cycle_id="c1", latency_ms=42.5)
    payload = json.loads(buf.getvalue())
    for field in (
        "timestamp", "level", "message", "cycle_id", "thread_id",
        "decision_id", "agent_name", "task", "model_alias", "provider",
        "latency_ms", "result_status", "error_code",
    ):
        assert field in payload
    assert payload["cycle_id"] == "c1"
    assert payload["latency_ms"] == 42.5


def test_json_formatter_never_leaks_secret_in_extra():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    logger = logging.getLogger("secret-extra-test")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    logger.info(f"cfg api_key={DUMMY_KEY}", extra={"detail": DUMMY_KEY})
    out = buf.getvalue()
    assert DUMMY_KEY not in out
    assert "****" in out


def test_setup_logging_idempotent():
    from monitoring.logger import setup_logging

    buf = io.StringIO()
    setup_logging("INFO", stream=buf)
    setup_logging("INFO", stream=buf)  # 不重复添加 handler
    lg = get_logger("setup-test")
    lg.info("one line")
    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    # 允许其它 handler 但至少本 handler 不重复输出同一行
    assert sum("one line" in l for l in lines) == 1


def test_setup_logging_file_writes_redacted_json(tmp_path):
    """file 参数写 JSON 行到磁盘,且脱敏同样生效(循环日志用途)。"""
    import logging as _logging

    import monitoring.logger as mlogger
    from monitoring.logger import setup_logging

    log_file = tmp_path / "agent_loop.log"
    root = _logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_configured = mlogger._configured
    root.handlers = []
    mlogger._configured = False
    try:
        setup_logging("INFO", file=log_file)
        get_logger("file-test").info(
            "cycle done", extra={"cycle_id": "c-1", "api_key": DUMMY_KEY}
        )
        for h in root.handlers:
            h.flush()
        content = log_file.read_text(encoding="utf-8")
        assert "cycle done" in content
        assert '"cycle_id": "c-1"' in content
        assert DUMMY_KEY not in content  # 文件与 stderr 同样脱敏
        assert "****" in content
    finally:
        for h in list(root.handlers):
            h.close()
        root.handlers = saved_handlers
        mlogger._configured = saved_configured


# ------------------------------------------------------------------ 指标


def test_metrics_counters_and_labels():
    m = MetricsCollector()
    m.record_model_call(alias="fast", ok=True, latency_ms=10)
    m.record_model_call(alias="fast", ok=False)
    snap = m.snapshot()
    assert snap["counters"]["llm_calls_total{model_alias=fast,status=ok}"] == 1
    assert snap["counters"]["llm_calls_total{model_alias=fast,status=error}"] == 1


def test_metrics_latency_stats():
    m = MetricsCollector()
    for v in (10, 20, 30):
        m.observe("x_ms", v)
    snap = m.snapshot()
    stats = snap["latencies"]["x_ms"]
    assert stats["count"] == 3
    assert stats["avg"] == 20.0
    assert stats["max"] == 30.0


def test_metrics_monitoring_coverage():
    """§12 监控清单逐项可记录。"""
    m = MetricsCollector()
    m.record_model_call(alias="fast", ok=True, latency_ms=5, input_tokens=1, output_tokens=2)
    m.record_market_latency("BTC/USDT", "15m", 100)
    m.record_agent_failure("market_agent")
    m.record_risk_rejection("d1", rule="kill_switch")
    m.record_order_status("FILLED", side="BUY")
    m.record_trade(side="BUY", symbol="BTC/USDT")
    m.record_account(1000.0, 900.0)
    m.record_kill_switch(True)
    m.record_cycle("executed")
    snap = m.snapshot()
    joined = json.dumps(snap, ensure_ascii=False)
    for fragment in (
        "llm_calls_total", "market_data_latency_ms", "agent_failures_total",
        "risk_rejections_total", "orders_total", "trades_total",
        "account_equity_usdt", "kill_switch_engaged", "cycles_total",
    ):
        assert fragment in joined


def test_metrics_thread_safe_snapshot():
    import threading

    m = MetricsCollector()

    def bump():
        for _ in range(100):
            m.incr("x")

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert m.snapshot()["counters"]["x"] == 400


# ------------------------------------------------------------------ Telegram


def _client_returning(status_code: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"ok": status_code < 400})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_telegram_send_only_posts_to_api():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    n = TelegramNotifier(
        TG_TOKEN, "42", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert n.send("hello") is True
    assert len(calls) == 1
    assert calls[0].url.path == f"/bot{TG_TOKEN}/sendMessage"
    assert calls[0].method == "POST"


def test_telegram_disabled_noop():
    n = TelegramNotifier("", "", client=_client_returning())
    assert n.enabled is False
    assert n.send("x") is False


def test_telegram_proxy_injected_when_no_client():
    """显式 proxy_url 覆盖一切:只挂 all:// 一个代理,不读环境/注册表。

    (httpx 经 trust_env 默认会读系统代理,挂载数随机器环境变化,
    所以只断言"显式配置"这条确定性路径;未配代理的挂载数不在此断言。)
    """
    n = TelegramNotifier(TG_TOKEN, "42", proxy_url="http://127.0.0.1:10808")
    assert len(n._client._mounts) == 1  # 显式代理 → 仅 all:// 一个挂载


def test_telegram_injected_client_wins_over_proxy():
    """测试注入的 client 优先于 proxy_url(离线测试不建真实代理连接)。"""
    n = TelegramNotifier(
        TG_TOKEN, "42", client=_client_returning(), proxy_url="http://127.0.0.1:10808"
    )
    assert n.send("x") is True


def test_telegram_failure_never_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    n = TelegramNotifier(
        TG_TOKEN, "42", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert n.send("x") is False  # 不抛异常,返回 False


def test_telegram_http_error_returns_false():
    n = TelegramNotifier(TG_TOKEN, "42", client=_client_returning(403))
    assert n.send("x") is False


def test_telegram_has_no_command_execution_surface():
    """仅发送:公开方法只有 send/send_cycle_summary/enabled,无任何交易接口。"""
    public = [m for m in dir(TelegramNotifier) if not m.startswith("_")]
    assert set(public) <= {"enabled", "send", "send_cycle_summary"}
    import inspect

    src = inspect.getsource(inspect.getmodule(TelegramNotifier))
    for banned in ("getUpdates", "webhook", "execute_order", "submit_order", "create_order"):
        assert banned not in src


def test_telegram_summary_message_has_no_secret():
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.content.decode())
        return httpx.Response(200, json={"ok": True})

    n = TelegramNotifier(
        TG_TOKEN, "42", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    n.send_cycle_summary(
        cycle_id="c1", symbol="BTC/USDT", final_status="executed",
        decision_id="cycle-c1", action="BUY", amount_usdt=10,
    )
    body = captured[0]
    assert "BTC/USDT" in body
    assert TG_TOKEN not in body  # token 只在 URL,不在消息体
    assert DUMMY_KEY not in body
