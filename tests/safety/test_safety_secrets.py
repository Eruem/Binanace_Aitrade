"""安全测试:密钥脱敏与安全默认值(指导书 14.17/14.18)。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from config.loader import is_secret_key, mask_secret

ROOT = Path(__file__).resolve().parents[2]

DUMMY_KEY = "sk-test-dummy-key-ABCDEF-98765"


def test_mask_never_reveals():
    assert mask_secret(DUMMY_KEY) == "****"
    assert DUMMY_KEY not in mask_secret(DUMMY_KEY)


def test_is_secret_key_detection():
    assert is_secret_key("api_key")
    assert is_secret_key("BINANCE_TESTNET_API_SECRET")
    assert is_secret_key("telegram_bot_token")
    assert not is_secret_key("base_url")
    assert not is_secret_key("model")


def test_check_config_output_has_no_key(tmp_path):
    """运行 check_config.py,断言输出不含测试密钥。"""
    env = os.environ.copy()
    env.update(
        {
            "LLM_PRIMARY_BASE_URL": "https://primary.example/v1",
            "LLM_PRIMARY_API_KEY": DUMMY_KEY,
            "LLM_SECONDARY_BASE_URL": "https://secondary.example/v1",
            "LLM_SECONDARY_API_KEY": DUMMY_KEY,
            "LLM_FAST_MODEL": "fast-model",
            "LLM_REASONING_MODEL": "reason-model",
            "LLM_BACKUP_MODEL": "backup-model",
            "TRADING_MODE": "paper",
            "ENABLE_LIVE_TRADING": "false",
            "ENABLE_BINANCE_TESTNET": "false",
        }
    )
    # 确保 .env 不会覆盖测试变量
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_config.py")],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert DUMMY_KEY not in out
    assert "****" in out


def test_check_config_fails_cleanly_when_key_missing(tmp_path):
    env = os.environ.copy()
    # 显式置空而非 pop:本地 .env 存在时会回填该变量,
    # 置空才能同时覆盖"无 .env"与"有 .env"两种环境(空值=缺失)
    env["LLM_PRIMARY_API_KEY"] = ""
    env["LLM_PRIMARY_BASE_URL"] = "https://primary.example/v1"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_config.py")],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1
    assert "LLM_PRIMARY_API_KEY" in (proc.stdout + proc.stderr)


def test_live_trading_env_rejected_by_main():
    env = os.environ.copy()
    env.update(
        {
            "LLM_PRIMARY_BASE_URL": "https://primary.example/v1",
            "LLM_PRIMARY_API_KEY": DUMMY_KEY,
            "LLM_SECONDARY_BASE_URL": "https://secondary.example/v1",
            "LLM_SECONDARY_API_KEY": DUMMY_KEY,
            "LLM_FAST_MODEL": "fast-model",
            "LLM_REASONING_MODEL": "reason-model",
            "LLM_BACKUP_MODEL": "backup-model",
            "TRADING_MODE": "paper",
            "ENABLE_LIVE_TRADING": "true",  # 危险开关
            "ENABLE_BINANCE_TESTNET": "false",
        }
    )
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py")],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1
    combined = proc.stdout + proc.stderr
    assert "ENABLE_LIVE_TRADING" in combined
    assert DUMMY_KEY not in combined


def test_main_runs_in_paper_mode():
    env = os.environ.copy()
    env.update(
        {
            "LLM_PRIMARY_BASE_URL": "https://primary.example/v1",
            "LLM_PRIMARY_API_KEY": DUMMY_KEY,
            "LLM_SECONDARY_BASE_URL": "https://secondary.example/v1",
            "LLM_SECONDARY_API_KEY": DUMMY_KEY,
            "LLM_FAST_MODEL": "fast-model",
            "LLM_REASONING_MODEL": "reason-model",
            "LLM_BACKUP_MODEL": "backup-model",
            "TRADING_MODE": "paper",
            "ENABLE_LIVE_TRADING": "false",
            "ENABLE_BINANCE_TESTNET": "false",
        }
    )
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py")],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "TRADING MODE: PAPER" in out
    assert "LIVE TRADING: DISABLED" in out
    assert DUMMY_KEY not in out
