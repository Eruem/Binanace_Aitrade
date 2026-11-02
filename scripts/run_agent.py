"""交易循环驱动脚本(阶段 8)。

把配置 → 行情 → LLM Agent → 风控 → 执行 → 审计全链路接起来,
是唯一被支持的"真正跑起来"入口(paper / testnet / live 通用)。

用法:
    python scripts/run_agent.py                    # 单轮(每个配置的交易对各一轮)
    python scripts/run_agent.py --loop --interval 300   # 常驻循环(秒)
    python scripts/run_agent.py --symbol BTC/USDT  # 只跑指定交易对

安全边界:
- 配置校验失败立即退出(fail-safe;绝不降级为其他模式);
- live(真实资金)需 TRADING_MODE=live + ENABLE_LIVE_TRADING=true +
  独立 Live 密钥 + risk.enabled=true 四要素齐备,启动时打印警告横幅;
- 风控引擎与执行层强制挂载(图装配保证:无风控绝不执行);
- 行情/交易流量经 EXCHANGE_PROXY_URL(若配置)注入 CCXT
  (CCXT 默认不读系统代理,trust_env=False);
- 本脚本不读取任何密钥,密钥只经 .env → 配置层注入客户端。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ConfigError, load_config  # noqa: E402
from config.loader import ConfigSet  # noqa: E402
from monitoring.logger import get_logger, setup_logging  # noqa: E402

_log = get_logger(__name__)


@dataclass
class Runtime:
    """一次装配好的完整运行时。"""

    cfg: ConfigSet
    graph: Any
    broker: Any
    market: Any
    router: Any


def build_runtime(cfg: ConfigSet, *, exchange=None, data_exchange=None) -> Runtime:
    """按配置装配全链路;exchange/data_exchange 仅供测试注入假对象。"""
    from agents.graph import build_graph
    from agents.nodes import NodeDeps
    from data.binance_data import BinancePublicData
    from database import build_recorder
    from exchange.factory import build_broker, build_order_manager
    from llm.router import ModelRouter
    from monitoring.metrics import collector
    from monitoring.telegram import build_notifier
    from risk.engine import RiskEngine

    proxy_url = cfg.settings.proxy.url or None
    market = BinancePublicData(exchange=data_exchange, proxy_url=proxy_url)
    router = ModelRouter.from_configset(cfg)
    broker = build_broker(cfg, exchange=exchange)
    manager = build_order_manager(cfg, broker)

    # 风控引擎强制挂载(live 模式下 risk.enabled=true 已由配置层保证)
    risk = RiskEngine(
        params=cfg.risk,
        allowed_symbols=cfg.trading_file.market.symbols,
        audit_path=str(Path(cfg.settings.app.data_dir) / "risk_audit.jsonl"),
    )

    tg = cfg.settings.telegram
    deps = NodeDeps(
        router=router,
        fetch_series=market.fetch_snapshot_series,
        audit_path=Path(cfg.settings.paths.audit_log),
        risk_engine=risk,
        get_account=broker.get_account,
        order_manager=manager,
        recorder=build_recorder(cfg.settings.database.url),
        metrics=collector,
        notifier=build_notifier(tg.bot_token, tg.chat_id, proxy_url=proxy_url),
    )
    return Runtime(
        cfg=cfg, graph=build_graph(deps), broker=broker, market=market, router=router
    )


async def aclose_runtime(rt: Runtime) -> None:
    """释放行情客户端与(若支持的)交易所客户端;不影响任何状态。"""
    try:
        await rt.market.aclose()
    except Exception:
        pass
    aclose = getattr(rt.broker, "aclose", None)
    if callable(aclose):
        try:
            await aclose()
        except Exception:
            pass


async def run_once(rt: Runtime, *, symbols: list[str], timeframes: list[str]) -> None:
    """每个交易对跑一轮完整工作流并打印结果摘要。"""
    from agents.graph import run_cycle

    started = int(time.time() * 1000)
    cycle_id = f"drive-{started}"
    for symbol in symbols:
        full_cycle_id = f"{cycle_id}-{symbol.replace('/', '')}"
        _log.info(
            "cycle_started",
            extra={"cycle_id": full_cycle_id, "symbol": symbol},
        )
        state = await run_cycle(
            rt.graph,
            cycle_id=full_cycle_id,
            symbol=symbol,
            timeframes=list(timeframes),
            started_at=started,
        )
        decision_id = f"cycle-{full_cycle_id}"
        risk = state.get("risk_result")
        verdict = risk.verdict if risk is not None else "n/a"
        final_status = state.get("final_status")
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {symbol} "
            f"final={final_status} risk={verdict} "
            f"decision_id={decision_id}"
        )
        _log.info(
            "cycle_completed",
            extra={
                "cycle_id": full_cycle_id,
                "symbol": symbol,
                "result_status": final_status,
                "risk_verdict": verdict,
                "decision_id": decision_id,
                "llm_errors": len(state.get("llm_errors") or []),
                "data_errors": len(state.get("data_errors") or []),
            },
        )


def _print_banner(cfg: ConfigSet) -> None:
    s = cfg.sanitized_summary()
    mode = s["trading_mode"]
    print("=" * 60)
    print(f"  {s['app']['name']}  |  mode={mode}  broker={s['execution']['broker']}")
    if mode == "live":
        print("  " + "!" * 56)
        print("  !! LIVE TRADING:真实资金交易已开启(项目所有者显式授权)")
        print("  !! 风控引擎强制开启;任何损失由账户所有者承担")
        print("  " + "!" * 56)
    px = s["proxy"]
    print(f"  交易所代理: {'on ' + px['url'] if px['enabled'] else 'off(直连)'}")
    print(f"  数据库: {'on' if s['database']['enabled'] else 'off(仅 JSONL)'}"
          f"  Telegram: {'on' if s['telegram']['enabled'] else 'off'}")
    print(f"  交易对: {', '.join(s['symbols'])}  周期: {', '.join(s['timeframes'])}")
    print("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(description="交易循环驱动(paper/testnet/live)")
    parser.add_argument("--loop", action="store_true", help="常驻循环(默认单轮)")
    parser.add_argument("--interval", type=int, default=300, help="循环间隔秒数")
    parser.add_argument("--symbol", action="append", help="覆盖交易对(可多次)")
    parser.add_argument("--once", action="store_true", help="单轮后退出(默认行为)")
    args = parser.parse_args()

    try:
        cfg = load_config()
    except ConfigError as e:
        print("[FATAL] 配置错误(信息已脱敏):", file=sys.stderr)
        print(str(e), file=sys.stderr)
        return 1

    # JSON 行日志:stderr(docker logs / 终端)+ var/agent_loop.log
    # (监控面板按其 mtime 判断循环活跃;脱敏由 formatter 保证)
    setup_logging(
        cfg.settings.app.log_level,
        file=Path(cfg.settings.app.data_dir) / "agent_loop.log",
    )
    _print_banner(cfg)
    rt = build_runtime(cfg)
    symbols = args.symbol or list(cfg.trading_file.market.symbols)
    timeframes = list(cfg.trading_file.market.timeframes)

    async def _loop() -> None:
        while True:
            await run_once(rt, symbols=symbols, timeframes=timeframes)
            if not args.loop:
                return
            print(f"  …休眠 {args.interval}s(--loop 模式;Ctrl+C 退出)")
            await asyncio.sleep(args.interval)

    try:
        asyncio.run(_loop())
    except KeyboardInterrupt:
        print("\n已收到中断,清理后退出…")
        asyncio.run(aclose_runtime(rt))
    else:
        asyncio.run(aclose_runtime(rt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
