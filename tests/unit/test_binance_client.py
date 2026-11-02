"""阶段 6:Binance Spot Testnet 客户端安全边界测试(指导书 9.4/13)。

必须确认连接的是 Testnet;正式 API Key 不会被读取;任何异常不携带密钥。
"""

from __future__ import annotations

import inspect
import time
from decimal import Decimal

import ccxt
import pytest

from exchange.binance_client import (
    TESTNET_URL_HINT,
    TIME_SYNC_INTERVAL_S,
    BinanceSpotClient,
    ExchangeAPIError,
    ExchangeNetworkError,
    SymbolRules,
    TestnetSafetyError,
    map_exchange_error,
    to_exchange_symbol,
)

KEY = "test-key"
SECRET = "test-secret"


class OrderNotFound(Exception):
    """模拟 ccxt OrderNotFound(按类名识别)。"""


class FakeExchange:
    """可注入的假交易所:记录调用,不发真实请求。

    urls 初始指向正式 API;set_sandbox_mode(True) 后切到 Testnet
    (模拟 ccxt 行为,用于验证 sandbox 先于任何请求)。
    """

    def __init__(self) -> None:
        self.urls = {"api": {"public": "https://api.binance.com"}}
        self.calls: list[tuple[str, dict]] = []
        self.sandbox: bool | None = None  # None = 从未设置
        self.post_order_response: dict = {
            "symbol": "BTCUSDT",
            "orderId": 1,
            "status": "FILLED",
            "clientOrderId": "cid",
            "side": "BUY",
            "executedQty": "0.01",
            "origQty": "0.01",
            "cummulativeQuoteQty": "500.00",
            "transactTime": 1793145600000,
        }
        self.get_order_response: dict | None = None
        self.get_order_error: Exception | None = None
        self.account_response: dict = {"balances": []}
        self.mytrades_response: list = []
        self.ticker_response: dict = {"price": "50000.00"}
        self.exchangeinfo_response: dict = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.00001000"},
                        {"filterType": "MIN_NOTIONAL", "minNotional": "5.0000"},
                    ],
                }
            ]
        }

    # -- 调用记录 ------------------------------------------------------
    def _rec(self, method: str, params: dict) -> None:
        # 每次调用先断言 sandbox 已启用:请求绝不先于安全开关
        self.calls.append((method, dict(params)))
        assert self.sandbox is True, f"{method} 在 set_sandbox_mode 之前被调用"

    # -- CCXT 接口 -----------------------------------------------------
    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox = enabled
        if enabled:
            self.urls = {"api": {"public": f"https://{TESTNET_URL_HINT}"}}

    async def close(self) -> None:
        pass

    async def private_post_order(self, params: dict) -> dict:
        self._rec("private_post_order", params)
        return dict(self.post_order_response)

    async def private_get_order(self, params: dict) -> dict:
        self._rec("private_get_order", params)
        if self.get_order_error is not None:
            raise self.get_order_error
        if self.get_order_response is None:
            raise OrderNotFound("Order does not exist.")
        return dict(self.get_order_response)

    async def private_delete_order(self, params: dict) -> dict:
        self._rec("private_delete_order", params)
        return {"status": "CANCELED", "symbol": "BTCUSDT", "executedQty": "0"}

    async def private_get_account(self, params: dict) -> dict:
        self._rec("private_get_account", params)
        return dict(self.account_response)

    async def private_get_mytrades(self, params: dict) -> list:
        self._rec("private_get_mytrades", params)
        return list(self.mytrades_response)

    async def public_get_ticker_price(self, params: dict) -> dict:
        # 名字必须与真实 ccxt 隐式方法一致(ticker/price → ticker_price)
        self._rec("public_get_ticker_price", params)
        return dict(self.ticker_response)

    async def public_get_exchangeinfo(self, params: dict) -> dict:
        self._rec("public_get_exchangeinfo", params)
        return dict(self.exchangeinfo_response)


class NoSandboxExchange(FakeExchange):
    """set_sandbox_mode 后 URL 仍指向正式 API(模拟配置被篡改)。"""

    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox = enabled  # 故意不切换 urls


def make_client(exchange: FakeExchange | None = None) -> tuple[BinanceSpotClient, FakeExchange]:
    ex = exchange or FakeExchange()
    return BinanceSpotClient(KEY, SECRET, testnet=True, exchange=ex), ex


# ----------------------------------------------------------------------
# 构造安全边界
# ----------------------------------------------------------------------
def test_testnet_false_rejected():
    """本版本不实现 Live:testnet=False 一律拒绝。"""
    with pytest.raises(TestnetSafetyError):
        BinanceSpotClient(KEY, SECRET, testnet=False, exchange=FakeExchange())


def test_empty_keys_rejected():
    with pytest.raises(TestnetSafetyError):
        BinanceSpotClient("", SECRET, testnet=True, exchange=FakeExchange())
    with pytest.raises(TestnetSafetyError):
        BinanceSpotClient(KEY, "", testnet=True, exchange=FakeExchange())


def test_sandbox_enabled_before_first_request():
    client, ex = make_client()
    assert ex.sandbox is True  # 构造时(首个请求前)已启用
    # FakeExchange._rec 在每次调用时断言 sandbox;能发出调用即证明顺序正确
    assert client.confirmed_testnet


def test_url_gate_rejects_non_testnet():
    """set_sandbox_mode 后 URL 不是 Testnet → 拒绝构造(拒绝一切请求)。"""
    with pytest.raises(TestnetSafetyError):
        BinanceSpotClient(KEY, SECRET, testnet=True, exchange=NoSandboxExchange())


def test_client_never_reads_env(monkeypatch):
    """客户端从不读取任何环境变量(正式 Key 绝不会被误用)。"""
    src = inspect.getsource(BinanceSpotClient)
    for forbidden in ("environ", "getenv"):
        assert forbidden not in src, f"客户端源码出现 {forbidden},违反不读环境变量约束"
    # 即使正式网环境变量存在也不影响构造(参数只来自显式传入)
    monkeypatch.setenv("BINANCE_API_KEY", "live-key-should-be-ignored")
    monkeypatch.setenv("BINANCE_API_SECRET", "live-secret-should-be-ignored")
    client, ex = make_client()
    assert ex.calls == []  # 构造不发出任何请求
    assert client.confirmed_testnet


def test_exception_never_carries_secret():
    """构造失败的异常信息不携带密钥。"""
    try:
        BinanceSpotClient(KEY, SECRET, testnet=False, exchange=FakeExchange())
    except TestnetSafetyError as e:
        assert KEY not in str(e) and SECRET not in str(e)
    else:  # pragma: no cover
        pytest.raises(AssertionError)


# ----------------------------------------------------------------------
# 符号与错误归类
# ----------------------------------------------------------------------
def test_to_exchange_symbol():
    assert to_exchange_symbol("BTC/USDT") == "BTCUSDT"
    assert to_exchange_symbol("BTC/USDT:USDT") == "BTCUSDT"
    assert to_exchange_symbol("btc/usdt") == "BTCUSDT"


def test_map_exchange_error_classification():
    net = ccxt.NetworkError("timeout")
    biz = ccxt.ExchangeError("insufficient balance")
    assert isinstance(map_exchange_error(net), ExchangeNetworkError)
    assert isinstance(map_exchange_error(biz), ExchangeAPIError)
    # 已归类的不再包装
    e1 = ExchangeNetworkError("x")
    assert map_exchange_error(e1) is e1


# ----------------------------------------------------------------------
# 请求参数与响应解析
# ----------------------------------------------------------------------
async def test_buy_uses_quote_order_qty_and_client_id():
    client, ex = make_client()
    out = await client.create_market_buy_quote(
        "BTC/USDT", quote_qty=Decimal("500.12345678"), client_order_id="ai-cid-1"
    )
    method, params = ex.calls[0]
    assert method == "private_post_order"
    assert params["symbol"] == "BTCUSDT"
    assert params["side"] == "BUY"
    assert params["type"] == "MARKET"
    assert params["quoteOrderQty"] == "500.12345678"
    assert params["newClientOrderId"] == "ai-cid-1"
    assert out["status"] == "FILLED"


async def test_sell_uses_quantity():
    client, ex = make_client()
    await client.create_market_sell(
        "BTC/USDT", quantity=Decimal("0.01000"), client_order_id="ai-cid-2"
    )
    _, params = ex.calls[0]
    assert params["side"] == "SELL"
    assert params["quantity"] == "0.01000"
    assert "quoteOrderQty" not in params


async def test_fetch_order_not_found_returns_none():
    client, ex = make_client()
    ex.get_order_error = OrderNotFound("Order does not exist.")
    assert await client.fetch_order("ai-cid", symbol="BTC/USDT") is None
    # 查询必须携带 origClientOrderId 与 symbol
    _, params = ex.calls[0]
    assert params == {"symbol": "BTCUSDT", "origClientOrderId": "ai-cid"}


async def test_fetch_order_maps_network_error():
    client, ex = make_client()
    ex.get_order_error = ccxt.RequestTimeout("timed out")
    with pytest.raises(ExchangeNetworkError):
        await client.fetch_order("ai-cid", symbol="BTC/USDT")


async def test_network_timeout_on_submit_raises_network_error():
    """网络超时 → ExchangeNetworkError 上抛(上层标记 UNKNOWN,不盲目重发)。"""
    client, ex = make_client()

    async def boom(params):
        ex.calls.append(("private_post_order", dict(params)))
        raise ccxt.RequestTimeout("timeout")

    ex.private_post_order = boom  # type: ignore[assignment]
    with pytest.raises(ExchangeNetworkError):
        await client.create_market_buy_quote(
            "BTC/USDT", quote_qty=Decimal("500"), client_order_id="ai-cid"
        )


async def test_api_rejection_raises_api_error():
    """交易所明确拒绝(余额不足)→ ExchangeAPIError(上层映射 REJECTED)。"""
    client, ex = make_client()

    async def boom(params):
        ex.calls.append(("private_post_order", dict(params)))
        raise ccxt.InsufficientFunds("Account has insufficient balance.")

    ex.private_post_order = boom  # type: ignore[assignment]
    with pytest.raises(ExchangeAPIError):
        await client.create_market_buy_quote(
            "BTC/USDT", quote_qty=Decimal("500"), client_order_id="ai-cid"
        )


async def test_fetch_account_filters_zero_balances():
    client, ex = make_client()
    ex.account_response = {
        "balances": [
            {"asset": "USDT", "free": "1000.00", "locked": "10.00"},
            {"asset": "BTC", "free": "0.00000000", "locked": "0.00000000"},
            {"asset": "ETH", "free": "0.5", "locked": "0"},
        ]
    }
    out = await client.fetch_account_balances()
    assert [b["asset"] for b in out] == ["USDT", "ETH"]
    assert out[0]["free"] == Decimal("1000.00")
    assert out[0]["locked"] == Decimal("10.00")


async def test_fetch_symbol_rules_parses_and_caches():
    client, ex = make_client()
    rules = await client.fetch_symbol_rules("BTC/USDT")
    assert rules == SymbolRules(
        symbol="BTC/USDT", amount_step=Decimal("0.00001000"), min_notional=Decimal("5.0000")
    )
    await client.fetch_symbol_rules("BTC/USDT")
    assert len(ex.calls) == 1  # 第二次走缓存,不再请求


async def test_fetch_symbol_rules_notion_filter_fallback():
    client, ex = make_client()
    ex.exchangeinfo_response = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                    {"filterType": "NOTIONAL", "minNotional": "1.00"},
                ],
            }
        ]
    }
    rules = await client.fetch_symbol_rules("BTC/USDT")
    assert rules.min_notional == Decimal("1.00")


async def test_fetch_symbol_rules_unknown_symbol():
    client, ex = make_client()
    ex.exchangeinfo_response = {"symbols": []}
    with pytest.raises(ExchangeAPIError):
        await client.fetch_symbol_rules("XXX/USDT")


async def test_fetch_ticker_price():
    client, _ = make_client()
    assert await client.fetch_ticker_price("BTC/USDT") == Decimal("50000.00")


async def test_fetch_ticker_rejects_nonpositive():
    client, ex = make_client()
    ex.ticker_response = {"price": "0"}
    with pytest.raises(ExchangeAPIError):
        await client.fetch_ticker_price("BTC/USDT")


async def test_mytrades_failure_returns_empty():
    """my_trades 查询失败不致命(手续费字段缺失);返回空列表。"""
    client, ex = make_client()

    async def boom(params):
        raise ccxt.NetworkError("timeout")

    ex.private_get_mytrades = boom  # type: ignore[assignment]
    assert await client.fetch_order_trades("BTC/USDT", "1") == []


async def test_default_exchange_proxy_injected():
    """阶段 8:客户端默认交易所显式注入代理(CCXT 不读系统代理)。"""
    client = BinanceSpotClient(
        KEY, SECRET, proxy_url="http://127.0.0.1:10808"
    )
    try:
        assert client._exchange.https_proxy == "http://127.0.0.1:10808"
        assert not client._exchange.http_proxy  # 不与 https_proxy 同设(冲突)
    finally:
        await client.aclose()


async def test_default_exchange_no_proxy_by_default():
    client = BinanceSpotClient(KEY, SECRET)
    try:
        assert not getattr(client._exchange, "https_proxy", None)
    finally:
        await client.aclose()


# ----------------------------------------------------------------------
# Live(阶段 8):显式 allow_live + 正式 API URL 确认,缺一不可
# ----------------------------------------------------------------------
def test_live_requires_allow_live_flag():
    """无 allow_live=True,testnet=False 依旧一律拒绝(默认 fail-safe)。"""
    with pytest.raises(TestnetSafetyError):
        BinanceSpotClient(KEY, SECRET, testnet=False, exchange=FakeExchange())


def test_live_constructed_with_allow_live_and_live_url():
    ex = FakeExchange()  # urls 初始指向 api.binance.com,从未 sandbox
    client = BinanceSpotClient(
        KEY, SECRET, testnet=False, allow_live=True, exchange=ex
    )
    assert ex.sandbox is None  # Live 绝不启用 sandbox
    assert client.name == "binance_spot_live"


def test_live_rejects_sandboxed_url():
    """上游把 URL 切到 Testnet 后再 allow_live → 拒绝(绝不误连沙箱)。"""
    ex = FakeExchange()
    ex.set_sandbox_mode(True)  # URL 变为 testnet.binance.vision
    with pytest.raises(TestnetSafetyError, match="Testnet"):
        BinanceSpotClient(KEY, SECRET, testnet=False, allow_live=True, exchange=ex)


def test_live_empty_keys_rejected():
    with pytest.raises(TestnetSafetyError):
        BinanceSpotClient(
            "", SECRET, testnet=False, allow_live=True, exchange=FakeExchange()
        )
    with pytest.raises(TestnetSafetyError):
        BinanceSpotClient(
            KEY, "", testnet=False, allow_live=True, exchange=FakeExchange()
        )


def test_live_accepts_real_ccxt_object_offline():
    """真实 ccxt 对象在 Live 下必须通过——URL 检查只看活跃区。

    回归背景:ccxt 的 urls 天生带 .test 模板区(含 testnet 主机,
    即使从未开 sandbox);早期对全量 urls 求 repr 导致真实对象
    在 Live 检查下永远误杀,而只含 api.binance.com 的测试桩测不出来。
    构造不发起任何网络请求,纯离线。
    """
    ex = ccxt.binance()
    client = BinanceSpotClient(
        KEY, SECRET, testnet=False, allow_live=True, exchange=ex
    )
    assert client.name == "binance_spot_live"


def test_testnet_accepts_real_ccxt_object_offline():
    """真实 ccxt 对象开 sandbox 后,活跃区确实指向 Testnet → 通过。"""
    ex = ccxt.binance()
    client = BinanceSpotClient(KEY, SECRET, testnet=True, exchange=ex)
    assert client.name == "binance_spot_testnet"
    # 活跃区必须已切换(不 sandbox 过不了确认)
    assert TESTNET_URL_HINT in repr(ex.urls["api"])


async def test_real_ccxt_exposes_every_implicit_method_client_uses():
    """客户端源码引用的每个隐式方法都必须存在于真实 ccxt(离线回归)。

    回归背景:客户端曾把 ticker/price 写成 public_get_tickerprice,
    真实 ccxt 上是 public_get_ticker_price;测试桩自造了错误的同名
    方法,单测全绿,首次真实私有调用(账户快照给持仓标记行情)
    才暴露 AttributeError。方法名清单直接从源码提取,新增调用自动覆盖。
    """
    import re

    import ccxt.async_support as ccxt_async

    src = inspect.getsource(BinanceSpotClient)
    used = sorted(set(re.findall(r"self\._exchange\.(\w+)", src)))
    assert used, "应至少从源码解析出一个交易所方法"
    ex = ccxt_async.binance()
    try:
        missing = [m for m in used if not hasattr(ex, m)]
        assert not missing, f"真实 ccxt 缺少客户端调用的方法: {missing}"
    finally:
        await ex.close()


# ----------------------------------------------------------------------
# 签名时间对拍(防 -1021 InvalidNonce:本机时钟无 NTP 会漂移)
# ----------------------------------------------------------------------


async def test_private_call_syncs_time_difference_once_per_interval():
    """私有请求前对拍服务器时间;冷却期内只拍一次,过期重拍。"""
    ex = FakeExchange()
    synced = {"n": 0}

    async def load_time_difference() -> int:
        synced["n"] += 1
        return 0

    ex.load_time_difference = load_time_difference  # 实例级动态挂载
    client, _ = make_client(ex)

    await client.fetch_account_balances()
    await client.fetch_account_balances()  # 冷却期内不再对拍
    assert synced["n"] == 1

    client._last_time_sync = time.monotonic() - (TIME_SYNC_INTERVAL_S + 1)
    await client.fetch_account_balances()
    assert synced["n"] == 2


async def test_time_sync_failure_does_not_break_private_call():
    """对拍失败(网络抖动)不阻断私有请求:签名失败由上层 fail-safe 兜底。"""
    ex = FakeExchange()
    ex.account_response = {
        "balances": [{"asset": "USDT", "free": "1.0", "locked": "0"}]
    }

    async def boom() -> None:
        raise RuntimeError("server time unavailable")

    ex.load_time_difference = boom
    client, _ = make_client(ex)

    out = await client.fetch_account_balances()  # 绝不因对拍失败抛错
    assert out == [
        {"asset": "USDT", "free": Decimal("1.0"), "locked": Decimal("0")}
    ]


async def test_time_sync_skipped_when_exchange_has_no_method():
    """注入的桩没有 load_time_difference 时静默跳过(现有桩全兼容)。"""
    client, _ = make_client()  # FakeExchange 无该方法
    await client._ensure_time_sync()  # 不抛即通过
    out = await client.fetch_account_balances()
    assert out == []
