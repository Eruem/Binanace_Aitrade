"""Binance Spot 客户端(阶段 6 Testnet;阶段 8 Live,项目所有者显式授权)。

安全边界(硬约束):
- 默认只连 Testnet:testnet=False 的构造必须显式 allow_live=True,否则拒绝;
- Testnet:set_sandbox_mode(True) 在首次请求前启用,并校验 URL 指向
  testnet.binance.vision,否则拒绝连接("必须确认连接的是 Testnet");
- Live:绝不启用 sandbox,且 URL 必须确认指向 api.binance.com,
  指向 Testnet 或未知地址一律拒绝;
- 只接受显式传入的 Key/Secret,**从不读取任何环境变量**;
- 任何异常信息都不携带密钥。

传输层:CCXT async_support 的私有/公共接口(签名自动);
交易所对象可注入(测试不发真实请求)。
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

TESTNET_URL_HINT = "testnet.binance.vision"
LIVE_URL_HINT = "api.binance.com"

# 签名前对拍服务器时间的冷却秒数:本机时钟未做 NTP 同步时会漂移
# (实测 Windows 时间源 = 主板 CMOS 时钟),偏差超过币安 ±1s 容差即
# -1021 InvalidNonce → 账户快照失败 → 风控整轮 fail-safe 否决
TIME_SYNC_INTERVAL_S = 300.0


class TestnetSafetyError(Exception):
    """违反 Testnet 安全约束(未确认 Testnet、尝试 Live、缺密钥等)。"""


class ExchangeNetworkError(Exception):
    """网络超时/断线:提交结果未知,上层标记 UNKNOWN(绝不盲目重发)。"""


class ExchangeAPIError(Exception):
    """交易所明确拒绝(余额不足/精度/最小名义等):上层映射为 REJECTED。"""


def _default_exchange(
    api_key: str, api_secret: str, proxy_url: str | None = None
) -> Any:
    try:
        import ccxt.async_support as ccxt_async
    except ImportError as e:  # pragma: no cover
        raise TestnetSafetyError(
            "未安装 ccxt;阶段 6 依赖: pip install -r requirements.txt"
        ) from e
    config: dict[str, Any] = {
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot",
            # ccxt 在 load_markets 时会自动对拍服务器时间;本项目不走
            # load_markets,主路径是 _ensure_time_sync 显式触发(双保险)
            "adjustForTimeDifference": True,
        },
    }
    ex = ccxt_async.binance(config)
    if proxy_url:
        # CCXT 默认 trust_env=False,不读系统/环境代理;被墙网络显式注入。
        # 用 https_proxy 属性(异步 aiohttp 只认 http_proxy/https_proxy,
        # 且两者同设会冲突;本项目流量全部为 https)
        ex.https_proxy = proxy_url
    return ex


def map_exchange_error(e: Exception) -> Exception:
    """把底层异常归类为网络错误 / 交易所拒绝;未知异常原样返回。"""
    if isinstance(e, (ExchangeNetworkError, ExchangeAPIError)):
        return e
    try:
        import ccxt

        if isinstance(e, ccxt.NetworkError):
            return ExchangeNetworkError(f"{type(e).__name__}: {e}")
        if isinstance(e, ccxt.ExchangeError):
            return ExchangeAPIError(f"{type(e).__name__}: {e}")
    except ImportError:  # pragma: no cover
        pass
    return e


@dataclass(frozen=True)
class SymbolRules:
    """交易所数量/名义规则(LOT_SIZE + MIN_NOTIONAL/NOTIONAL)。"""

    symbol: str
    amount_step: Decimal
    min_notional: Decimal


def to_exchange_symbol(symbol: str) -> str:
    """BTC/USDT -> BTCUSDT;BTC/USDT:USDT(统一格式结算后缀)同样 -> BTCUSDT。"""
    base = symbol.split(":", 1)[0]  # 丢弃结算资产后缀(现货无意义)
    return base.replace("/", "").upper()


class BinanceSpotClient:
    """Testnet Spot 客户端;薄传输层,不做业务决策。"""

    name = "binance_spot_testnet"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        testnet: bool = True,
        exchange: Any = None,
        proxy_url: str | None = None,
        allow_live: bool = False,
    ) -> None:
        if not testnet and not allow_live:
            # Live 连接必须显式授权(阶段 8;默认拒绝延续 fail-safe)
            raise TestnetSafetyError(
                "testnet=False 需要显式 allow_live=True;默认拒绝真实资金连接"
            )
        if not api_key or not api_secret:
            raise TestnetSafetyError(
                "Live API Key/Secret 不能为空"
                if not testnet
                else "Testnet API Key/Secret 不能为空"
            )
        self._exchange = (
            exchange
            if exchange is not None
            else _default_exchange(api_key, api_secret, proxy_url)
        )
        self._owns_exchange = exchange is None
        self._rules_cache: dict[str, SymbolRules] = {}
        self._last_time_sync: float = 0.0
        if testnet:
            # sandbox 模式必须在首次请求前启用
            self._exchange.set_sandbox_mode(True)
            self._confirm_testnet()
            self.name = "binance_spot_testnet"
        else:
            # Live:绝不启用 sandbox;URL 必须确认指向正式 API
            self._confirm_live()
            self.name = "binance_spot_live"

    def _active_api_urls(self) -> str:
        """活跃 API URL 区的 repr(真正会被请求的主机)。

        必须只看 urls["api"]:ccxt 的 urls 天生带 .test 模板区
        (即使从未开启 sandbox),对全量 urls 求 repr 会把休眠的
        testnet 主机一并算进来,Live 检查将永远误杀;反过来,
        sandbox 开启后 ccxt 会把 test 主机拷入 urls["api"],
        活跃区因此才是"是否真指向 Testnet"的可靠依据。
        """
        urls = getattr(self._exchange, "urls", None) or {}
        api = urls.get("api", urls) if isinstance(urls, dict) else urls
        try:
            return repr(api)
        except Exception:
            return str(api)

    def _confirm_testnet(self) -> None:
        """确认活跃 URL 指向 Testnet(校验 URL);不满足即拒绝任何请求。"""
        blob = self._active_api_urls()
        if TESTNET_URL_HINT not in blob:
            raise TestnetSafetyError(
                f"未能确认连接的是 Testnet(期望 URL 包含 {TESTNET_URL_HINT});拒绝请求"
            )

    def _confirm_live(self) -> None:
        """Live:活跃 URL 必须指向正式 API,且绝不允许 Testnet URL。"""
        blob = self._active_api_urls()
        if TESTNET_URL_HINT in blob:
            raise TestnetSafetyError(
                f"Live 连接的 URL 指向 Testnet({TESTNET_URL_HINT});拒绝请求"
            )
        if LIVE_URL_HINT not in blob:
            raise TestnetSafetyError(
                f"未能确认连接的是正式 API(期望 URL 包含 {LIVE_URL_HINT});拒绝请求"
            )

    @property
    def confirmed_testnet(self) -> bool:
        return True  # 构造时已确认,否则实例不存在

    async def aclose(self) -> None:
        if self._owns_exchange:
            await self._exchange.close()

    # ------------------------------------------------------------------
    # 错误包装:网络类 → ExchangeNetworkError;业务拒绝 → ExchangeAPIError
    # ------------------------------------------------------------------
    async def _call(self, fn, /, *args, **kwargs) -> Any:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            raise map_exchange_error(e) from e

    async def _ensure_time_sync(self) -> None:
        """签名前把 ccxt 的 timeDifference 对拍到交易所服务器时间。

        本机时钟无 NTP 同步时会漂移,偏差 ≥1s 即 -1021 InvalidNonce,
        整轮 fail-safe 否决(2026-11-02 实测:Windows 时间源为 CMOS
        时钟,14:45-16:09 连续多轮被否决)。每 TIME_SYNC_INTERVAL_S
        秒至多重拍一次;失败保留旧值、不阻断请求(签名真失败由上层
        fail-safe 兜底)。测试桩可能没有该方法或为同步实现,均兼容。
        """
        now = time.monotonic()
        if now - self._last_time_sync < TIME_SYNC_INTERVAL_S:
            return
        loader = getattr(self._exchange, "load_time_difference", None)
        if loader is None:
            return
        try:
            result = loader()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass
        finally:
            self._last_time_sync = now

    async def _call_private(self, fn, /, *args, **kwargs) -> Any:
        """私有(签名)请求:先对拍服务器时间,再走统一错误包装。"""
        await self._ensure_time_sync()
        return await self._call(fn, *args, **kwargs)

    # ------------------------------------------------------------------
    # 私有接口(签名请求)
    # ------------------------------------------------------------------
    async def create_market_buy_quote(
        self, symbol: str, *, quote_qty: Decimal, client_order_id: str
    ) -> dict:
        """BUY:按报价金额(quoteOrderQty)市价买入,精确花费 N USDT。"""
        return await self._call_private(
            self._exchange.private_post_order,
            {
                "symbol": to_exchange_symbol(symbol),
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": f"{quote_qty:f}",
                "newClientOrderId": client_order_id,
                "newOrderRespType": "RESULT",
            },
        )

    async def create_market_sell(
        self, symbol: str, *, quantity: Decimal, client_order_id: str
    ) -> dict:
        """SELL:按基础资产数量市价卖出。"""
        return await self._call_private(
            self._exchange.private_post_order,
            {
                "symbol": to_exchange_symbol(symbol),
                "side": "SELL",
                "type": "MARKET",
                "quantity": f"{quantity:f}",
                "newClientOrderId": client_order_id,
                "newOrderRespType": "RESULT",
            },
        )

    async def fetch_order(self, client_order_id: str, *, symbol: str) -> dict | None:
        """按 Client Order ID 查询;交易所无此单返回 None。"""
        await self._ensure_time_sync()
        try:
            return await self._exchange.private_get_order(
                {
                    "symbol": to_exchange_symbol(symbol),
                    "origClientOrderId": client_order_id,
                }
            )
        except Exception as e:
            if type(e).__name__ == "OrderNotFound" or "does not exist" in str(e).lower():
                return None
            raise map_exchange_error(e) from e

    async def cancel_order(self, client_order_id: str, *, symbol: str) -> dict:
        return await self._call_private(
            self._exchange.private_delete_order,
            {
                "symbol": to_exchange_symbol(symbol),
                "origClientOrderId": client_order_id,
            },
        )

    async def fetch_account_balances(self) -> list[dict]:
        """GET /api/v3/account → [{asset, free, locked}, ...](仅非零)。"""
        raw = await self._call_private(self._exchange.private_get_account, {})
        out = []
        for b in raw.get("balances", []):
            free = Decimal(str(b.get("free", "0")))
            locked = Decimal(str(b.get("locked", "0")))
            if free > 0 or locked > 0:
                out.append(
                    {"asset": b["asset"], "free": free, "locked": locked}
                )
        return out

    async def fetch_order_trades(self, symbol: str, order_id: str) -> list[dict]:
        """my_trades(按 orderId 过滤)→ 手续费明细;失败返回空(不致命)。"""
        await self._ensure_time_sync()
        try:
            raw = await self._exchange.private_get_mytrades(
                {
                    "symbol": to_exchange_symbol(symbol),
                    "orderId": str(order_id),
                }
            )
            return list(raw)
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 公共接口(无需签名)
    # ------------------------------------------------------------------
    async def fetch_ticker_price(self, symbol: str) -> Decimal:
        # 真实 ccxt 对 ticker/price 的隐式方法名是 public_get_ticker_price
        # (路径段带斜杠 → ticker_price);测试桩曾用错误的
        # public_get_tickerprice 掩盖此问题,首次真实私有调用才暴露
        raw = await self._call(
            self._exchange.public_get_ticker_price,
            {"symbol": to_exchange_symbol(symbol)},
        )
        price = Decimal(str(raw["price"]))
        if price <= 0:
            raise ExchangeAPIError(f"非法 ticker 价格: {price}")
        return price

    async def fetch_symbol_rules(self, symbol: str) -> SymbolRules:
        """LOT_SIZE.stepSize + MIN_NOTIONAL/NOTIONAL.minNotional(缓存)。"""
        if symbol in self._rules_cache:
            return self._rules_cache[symbol]
        raw = await self._call(
            self._exchange.public_get_exchangeinfo,
            {"symbol": to_exchange_symbol(symbol)},
        )
        symbols = raw.get("symbols", [])
        if not symbols:
            raise ExchangeAPIError(f"交易所无此交易对: {symbol}")
        filters = {f["filterType"]: f for f in symbols[0].get("filters", [])}
        step = Decimal(
            str(filters.get("LOT_SIZE", {}).get("stepSize", "0.00000001"))
        )
        notional = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
        min_notional = Decimal(str(notional.get("minNotional", "0")))
        if step <= 0:
            raise ExchangeAPIError(f"非法 stepSize: {step}")
        rules = SymbolRules(
            symbol=symbol, amount_step=step, min_notional=min_notional
        )
        self._rules_cache[symbol] = rules
        return rules
