"""应用入口(阶段 0 起持续维护)。

当前版本行为:
1. 加载并校验配置(失败则打印脱敏错误并退出非 0);
2. 打印交易模式横幅(paper/testnet);
3. 打印已实现组件清单。

本入口只做配置校验与横幅展示,不启动交易循环;各阶段组件
(行情/Agent/风控/执行)由 tests 与后续阶段的调度器接入。
无论配置如何,本版本都不会创建真实下单客户端(无 Live Broker)。
"""

from __future__ import annotations

import sys

from config import ConfigError, load_config

IMPLEMENTED = [
    "阶段 0:配置加载/校验/脱敏、目录骨架",
    "阶段 1:LLM Adapter(Provider/Registry/Router/复杂度/备用/解析/用量)",
    "阶段 2:行情与指标(CCXT 公共行情、数据校验、指标计算)",
    "阶段 3:Agent 与 LangGraph(三 Agent 并行工作流、Checkpointer、HOLD 安全降级)",
    "阶段 4:Risk Engine(11 条确定性规则、仓位计算、Kill Switch、日亏损、审计)",
    "阶段 5:Paper Broker(Broker 接口、订单状态机、模拟费/滑点/部分成交、幂等与持久化)",
    "阶段 6:Binance Spot Testnet(显式启用、独立 Testnet 密钥、订单对账、UNKNOWN 恢复)",
    "阶段 7:数据库/监控/通知(11 表审计入库、决策全链路追踪、结构化日志脱敏、"
    "指标收集、Telegram 只发通知)",
    "阶段 8:交易所代理注入、Live 真实资金交易(显式双开关+独立密钥+强制风控)、"
    "驱动循环脚本 scripts/run_agent.py",
]
NOT_IMPLEMENTED: list[str] = []


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        print("[FATAL] 配置错误(信息已脱敏):", file=sys.stderr)
        print(str(e), file=sys.stderr)
        return 1

    summary = cfg.sanitized_summary()
    print("=" * 60)
    print(f"  {summary['app']['name']}  |  env={summary['app']['env']}")
    print(f"  TRADING MODE: {summary['trading_mode'].upper()}")
    if summary["trading_mode"] == "live":
        print("  LIVE TRADING: *** ENABLED —真实资金交易,风控强制开启 ***")
    else:
        print("  LIVE TRADING: DISABLED(需要 TRADING_MODE=live 显式开启)")
    if summary["trading_mode"] == "testnet":
        print("  BINANCE TESTNET: enabled(需要独立 Testnet API Key)")
    print(
        "  DATABASE:",
        "enabled(审计入库;迁移用 python scripts/migrate_db.py)"
        if summary["database"]["enabled"] else "disabled(仅本地 JSONL 审计)",
    )
    print(
        "  TELEGRAM:",
        "enabled(仅发送通知)" if summary["telegram"]["enabled"] else "disabled",
    )
    print("=" * 60)
    print("已实现组件:")
    for item in IMPLEMENTED:
        print(f"  + {item}")
    if NOT_IMPLEMENTED:
        print("尚未实现(按阶段推进):")
        for item in NOT_IMPLEMENTED:
            print(f"  - {item}")
    print()
    print("当前启动不会连接任何交易所、不会创建下单客户端。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
