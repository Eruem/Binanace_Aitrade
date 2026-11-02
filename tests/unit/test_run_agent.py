"""阶段 8:驱动脚本 scripts/run_agent.py 装配测试(全离线)。

验证:paper 装配挂载风控+执行层、live 装配使用 LiveBroker、
paper 绝不触碰交易所客户端、CLI 可用。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from test_broker_factory import (  # 同目录复用夹具
    LiveOKExchange,
    PoisonExchange,
    make_cfg,
    make_live_cfg,
)

from exchange.paper_broker import PaperBroker

ROOT = Path(__file__).resolve().parents[2]


def _graph_node_names(graph) -> set[str]:
    nodes = getattr(graph, "nodes", None)
    if isinstance(nodes, dict):
        return set(nodes.keys())
    g = graph.get_graph()
    nodes = g.nodes
    if isinstance(nodes, dict):
        return set(nodes.keys())
    return {getattr(n, "name", str(n)) for n in nodes}


def test_paper_runtime_wires_risk_and_execute(tmp_path):
    from scripts.run_agent import build_runtime

    cfg = make_cfg(mode="paper", root=tmp_path)
    rt = build_runtime(cfg, data_exchange=object())
    assert isinstance(rt.broker, PaperBroker)
    names = _graph_node_names(rt.graph)
    # 无风控放行绝不执行:装配里 risk 与 execute 必须挂载
    assert "risk_node" in names
    assert "execute_node" in names


def test_live_runtime_builds_live_broker(tmp_path):
    from scripts.run_agent import build_runtime

    cfg = make_live_cfg(tmp_path)
    rt = build_runtime(cfg, exchange=LiveOKExchange(), data_exchange=object())
    assert rt.broker.name == "binance_live_broker"


def test_paper_runtime_never_touches_exchange(tmp_path):
    from scripts.run_agent import build_runtime

    cfg = make_cfg(mode="paper", root=tmp_path)
    # PoisonExchange:任何方法调用即失败;装配成功 = 从未被触碰
    rt = build_runtime(
        cfg, exchange=PoisonExchange(), data_exchange=PoisonExchange()
    )
    assert isinstance(rt.broker, PaperBroker)


def test_cli_help_exits_zero():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_agent.py"), "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0
    assert "--loop" in proc.stdout
