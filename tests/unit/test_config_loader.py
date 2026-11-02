"""阶段 0:配置加载/插值/校验测试。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from config.loader import ConfigError, interpolate_env, load_config, mask_secret

FAKE_ENV = {
    "LLM_PRIMARY_BASE_URL": "https://primary.example/v1",
    "LLM_PRIMARY_API_KEY": "sk-primary-123",
    "LLM_SECONDARY_BASE_URL": "https://secondary.example/v1",
    "LLM_SECONDARY_API_KEY": "sk-secondary-456",
    "LLM_FAST_MODEL": "fast-model-id",
    "LLM_REASONING_MODEL": "reason-model-id",
    "LLM_BACKUP_MODEL": "backup-model-id",
}


def _write_project(tmp: Path, *, settings_extra: str = "") -> Path:
    cfg = tmp / "config"
    cfg.mkdir(parents=True)
    (cfg / "settings.yaml").write_text(
        textwrap.dedent(
            """
            app:
              name: test-app
            trading:
              mode: ${TRADING_MODE:-paper}
              enable_live_trading: ${ENABLE_LIVE_TRADING:-false}
              enable_binance_testnet: ${ENABLE_BINANCE_TESTNET:-false}
            """
        )
        + settings_extra,
        encoding="utf-8",
    )
    (cfg / "models.yaml").write_text(
        textwrap.dedent(
            """
            providers:
              primary:
                base_url: ${LLM_PRIMARY_BASE_URL}
                api_key: ${LLM_PRIMARY_API_KEY}
                timeout: 45
              secondary:
                base_url: ${LLM_SECONDARY_BASE_URL}
                api_key: ${LLM_SECONDARY_API_KEY}
                timeout: 45
            models:
              fast:
                provider: primary
                model: ${LLM_FAST_MODEL}
                temperature: 0.2
                max_tokens: 1500
                supports_json: true
              reasoning:
                provider: primary
                model: ${LLM_REASONING_MODEL}
                temperature: 0.1
                max_tokens: 3000
                supports_json: true
              backup:
                provider: secondary
                model: ${LLM_BACKUP_MODEL}
                temperature: 0.1
                max_tokens: 2000
                supports_json: true
            routing:
              market_summary: fast
              market_analysis: fast
              strategy_analysis: reasoning
              decision: reasoning
              anomaly_analysis: reasoning
              risk_explanation: fast
            fallback:
              enabled: true
              model: backup
              max_attempts: 2
              on_all_models_failed: HOLD
            limits:
              max_calls_per_cycle: 8
              max_concurrent_requests: 3
              complexity_threshold: 3
            """
        ),
        encoding="utf-8",
    )
    (cfg / "trading.yaml").write_text(
        textwrap.dedent(
            """
            market:
              exchange: binance
              market_type: spot
              default_symbol: BTC/USDT
              symbols: [BTC/USDT]
              timeframes: [15m, 1h, 4h]
            strategies:
              candidates: [DCA, TREND_FOLLOWING, NO_TRADE]
            execution:
              mode: ${TRADING_MODE:-paper}
              testnet:
                api_key: ${BINANCE_TESTNET_API_KEY:-}
                api_secret: ${BINANCE_TESTNET_API_SECRET:-}
              live:
                api_key: ${BINANCE_LIVE_API_KEY:-}
                api_secret: ${BINANCE_LIVE_API_SECRET:-}
            """
        ),
        encoding="utf-8",
    )
    (cfg / "risk.yaml").write_text(
        textwrap.dedent(
            """
            risk:
              enabled: ${RISK_ENABLED:-true}
              max_single_order_equity_pct: 0.10
              max_total_exposure_pct: 0.30
              max_single_trade_risk_pct: 0.005
              max_daily_loss_pct: 0.02
              max_consecutive_losses: 3
              reject_expired_signal: true
              reject_duplicate_order: true
              require_closed_candle: true
              stop_on_data_error: true
              assumed_stop_loss_pct: 0.02
              fee_rate: 0.001
              slippage_rate: 0.0005
              min_order_usdt: 5.0
            """
        ),
        encoding="utf-8",
    )
    return tmp


def _load(tmp: Path, env: dict[str, str]) -> object:
    import os

    old = {k: os.environ.get(k) for k in FAKE_ENV}
    try:
        for k, v in env.items():
            os.environ[k] = v
        for k in FAKE_ENV:
            if k not in env:
                os.environ.pop(k, None)
        return load_config(tmp / "config", project_root=tmp)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestInterpolate:
    def test_simple_var(self):
        missing: list[str] = []
        out = interpolate_env("x=${FOO}", {"FOO": "bar"}, source="t", missing=missing)
        assert out == "x=bar"
        assert missing == []

    def test_default_used_when_missing(self):
        missing: list[str] = []
        out = interpolate_env(
            "${FOO:-fallback}", {}, source="t", missing=missing
        )
        assert out == "fallback"
        assert missing == []

    def test_missing_recorded_by_name_not_value(self):
        missing: list[str] = []
        out = interpolate_env("${SECRET_TOKEN}", {}, source="cfg.yaml", missing=missing)
        assert out == ""
        assert missing == ["cfg.yaml: 环境变量 SECRET_TOKEN 未设置"]


class TestLoadConfig:
    def test_full_load_ok(self, tmp_path):
        _write_project(tmp_path)
        cfg = _load(tmp_path, FAKE_ENV)
        assert cfg.trading_mode == "paper"
        assert cfg.models["fast"].model == "fast-model-id"
        assert cfg.routing["decision"] == "reasoning"
        assert cfg.fallback.model == "backup"

    def test_missing_env_var_fails_with_name(self, tmp_path):
        _write_project(tmp_path)
        env = {k: v for k, v in FAKE_ENV.items() if k != "LLM_PRIMARY_API_KEY"}
        with pytest.raises(ConfigError) as ei:
            _load(tmp_path, env)
        msg = str(ei.value)
        assert "LLM_PRIMARY_API_KEY" in msg
        assert ".env.example" in msg

    def test_live_trading_rejected(self, tmp_path):
        _write_project(tmp_path)
        import os

        os.environ["ENABLE_LIVE_TRADING"] = "true"
        try:
            with pytest.raises(ConfigError) as ei:
                _load(tmp_path, FAKE_ENV)
            assert "ENABLE_LIVE_TRADING" in str(ei.value)
        finally:
            os.environ.pop("ENABLE_LIVE_TRADING", None)

    def test_testnet_requires_flag(self, tmp_path):
        _write_project(tmp_path)
        import os

        os.environ["TRADING_MODE"] = "testnet"
        try:
            with pytest.raises(ConfigError) as ei:
                _load(tmp_path, FAKE_ENV)
            assert "ENABLE_BINANCE_TESTNET" in str(ei.value)
        finally:
            os.environ.pop("TRADING_MODE", None)

    def test_testnet_with_flag_ok(self, tmp_path):
        _write_project(tmp_path)
        import os

        os.environ["TRADING_MODE"] = "testnet"
        os.environ["ENABLE_BINANCE_TESTNET"] = "true"
        os.environ["BINANCE_TESTNET_API_KEY"] = "tn-key"
        os.environ["BINANCE_TESTNET_API_SECRET"] = "tn-secret"
        try:
            cfg = _load(tmp_path, FAKE_ENV)
            assert cfg.trading_mode == "testnet"
            assert cfg.trading_file.execution.derived_broker == "binance_testnet_broker"
        finally:
            os.environ.pop("TRADING_MODE", None)
            os.environ.pop("ENABLE_BINANCE_TESTNET", None)
            os.environ.pop("BINANCE_TESTNET_API_KEY", None)
            os.environ.pop("BINANCE_TESTNET_API_SECRET", None)

    def test_testnet_requires_keys(self, tmp_path):
        """阶段 6:testnet 模式缺 Testnet 密钥 → 启动拒绝(报错只含变量名)。"""
        _write_project(tmp_path)
        import os

        os.environ["TRADING_MODE"] = "testnet"
        os.environ["ENABLE_BINANCE_TESTNET"] = "true"
        try:
            with pytest.raises(ConfigError) as ei:
                _load(tmp_path, FAKE_ENV)
            msg = str(ei.value)
            assert "BINANCE_TESTNET_API_KEY" in msg
            assert "BINANCE_TESTNET_API_SECRET" in msg
        finally:
            os.environ.pop("TRADING_MODE", None)
            os.environ.pop("ENABLE_BINANCE_TESTNET", None)

    def test_execution_mode_must_match_trading_mode(self, tmp_path):
        """配置意外指向错误环境:execution.mode 与 TRADING_MODE 不一致 → 拒绝。"""
        _write_project(tmp_path)
        p = tmp_path / "config" / "trading.yaml"
        p.write_text(
            p.read_text(encoding="utf-8").replace(
                "mode: ${TRADING_MODE:-paper}", "mode: paper"
            ),
            encoding="utf-8",
        )
        import os

        os.environ["TRADING_MODE"] = "testnet"
        os.environ["ENABLE_BINANCE_TESTNET"] = "true"
        os.environ["BINANCE_TESTNET_API_KEY"] = "tn-key"
        os.environ["BINANCE_TESTNET_API_SECRET"] = "tn-secret"
        try:
            with pytest.raises(ConfigError) as ei:
                _load(tmp_path, FAKE_ENV)
            assert "execution.mode" in str(ei.value)
        finally:
            os.environ.pop("TRADING_MODE", None)
            os.environ.pop("ENABLE_BINANCE_TESTNET", None)
            os.environ.pop("BINANCE_TESTNET_API_KEY", None)
            os.environ.pop("BINANCE_TESTNET_API_SECRET", None)

    def test_testnet_keys_masked_in_summary(self, tmp_path):
        """Testnet 密钥在脱敏摘要中只显示 ****。"""
        _write_project(tmp_path)
        import os

        os.environ["TRADING_MODE"] = "testnet"
        os.environ["ENABLE_BINANCE_TESTNET"] = "true"
        os.environ["BINANCE_TESTNET_API_KEY"] = "tn-key-secret-value"
        os.environ["BINANCE_TESTNET_API_SECRET"] = "tn-secret-value"
        try:
            cfg = _load(tmp_path, FAKE_ENV)
            summary = cfg.sanitized_summary()
            ex = summary["execution"]
            assert ex["mode"] == "testnet"
            assert ex["broker"] == "binance_testnet_broker"
            assert ex["testnet"]["api_key"] == "****"
            assert ex["testnet"]["api_secret"] == "****"
            blob = str(summary)
            assert "tn-key-secret-value" not in blob
            assert "tn-secret-value" not in blob
        finally:
            os.environ.pop("TRADING_MODE", None)
            os.environ.pop("ENABLE_BINANCE_TESTNET", None)
            os.environ.pop("BINANCE_TESTNET_API_KEY", None)
            os.environ.pop("BINANCE_TESTNET_API_SECRET", None)

    def test_routing_unknown_alias_rejected(self, tmp_path):
        _write_project(tmp_path)
        p = tmp_path / "config" / "models.yaml"
        p.write_text(
            p.read_text(encoding="utf-8").replace(
                "market_summary: fast", "market_summary: nonexistent"
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError) as ei:
            _load(tmp_path, FAKE_ENV)
        assert "nonexistent" in str(ei.value)

    def test_on_all_models_failed_must_be_hold(self, tmp_path):
        _write_project(tmp_path)
        p = tmp_path / "config" / "models.yaml"
        p.write_text(
            p.read_text(encoding="utf-8").replace(
                "on_all_models_failed: HOLD", "on_all_models_failed: BUY"
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            _load(tmp_path, FAKE_ENV)

    def test_non_spot_market_rejected(self, tmp_path):
        _write_project(tmp_path)
        p = tmp_path / "config" / "trading.yaml"
        p.write_text(
            p.read_text(encoding="utf-8").replace("market_type: spot", "market_type: futures"),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            _load(tmp_path, FAKE_ENV)


class TestMask:
    def test_mask_secret(self):
        assert mask_secret("") == "(missing)"
        assert mask_secret("sk-abcdef") == "****"
        assert "abcdef" not in mask_secret("sk-abcdef")

    def test_sanitized_summary_has_no_key(self, tmp_path):
        _write_project(tmp_path)
        cfg = _load(tmp_path, FAKE_ENV)
        s = str(cfg.sanitized_summary())
        assert "sk-primary-123" not in s
        assert "sk-secondary-456" not in s


class TestPhase7Sections:
    """阶段 7:database/telegram 配置段。"""

    def test_database_disabled_by_default(self, tmp_path):
        _write_project(tmp_path)
        cfg = _load(tmp_path, FAKE_ENV)
        assert cfg.settings.database.enabled is False
        assert cfg.sanitized_summary()["database"]["enabled"] is False

    def test_database_url_scheme_rejected(self, tmp_path):
        _write_project(
            tmp_path,
            settings_extra="""
database:
  url: ${DATABASE_URL:-}
""",
        )
        import os

        os.environ["DATABASE_URL"] = "mysql://user:pw@host/db"
        try:
            with pytest.raises(ConfigError) as ei:
                _load(tmp_path, FAKE_ENV)
            assert "database.url" in str(ei.value)
            assert "pw" not in str(ei.value)
        finally:
            os.environ.pop("DATABASE_URL", None)

    def test_database_url_masked_in_summary(self, tmp_path):
        _write_project(
            tmp_path,
            settings_extra="""
database:
  url: ${DATABASE_URL:-}
""",
        )
        import os

        os.environ["DATABASE_URL"] = (
            "postgresql+psycopg://agent:super-secret-pw@db:5432/crypto_agent"
        )
        try:
            cfg = _load(tmp_path, FAKE_ENV)
            summary = cfg.sanitized_summary()
            assert summary["database"]["enabled"] is True
            blob = str(summary)
            assert "super-secret-pw" not in blob
            assert "****" in summary["database"]["url"]
        finally:
            os.environ.pop("DATABASE_URL", None)

    def test_sqlite_url_allowed(self, tmp_path):
        _write_project(
            tmp_path,
            settings_extra="""
database:
  url: ${DATABASE_URL:-}
""",
        )
        import os

        os.environ["DATABASE_URL"] = "sqlite:///var/agent.db"
        try:
            cfg = _load(tmp_path, FAKE_ENV)
            assert cfg.settings.database.enabled is True
        finally:
            os.environ.pop("DATABASE_URL", None)

    def test_telegram_disabled_by_default(self, tmp_path):
        _write_project(tmp_path)
        cfg = _load(tmp_path, FAKE_ENV)
        assert cfg.settings.telegram.enabled is False

    def test_telegram_partial_config_rejected(self, tmp_path):
        _write_project(
            tmp_path,
            settings_extra="""
telegram:
  bot_token: ${TELEGRAM_BOT_TOKEN:-}
  chat_id: ${TELEGRAM_CHAT_ID:-}
""",
        )
        import os

        os.environ["TELEGRAM_BOT_TOKEN"] = "123456:AAAtoken"
        try:
            with pytest.raises(ConfigError) as ei:
                _load(tmp_path, FAKE_ENV)
            assert "TELEGRAM_BOT_TOKEN" in str(ei.value)
            assert "AAAtoken" not in str(ei.value)
        finally:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)

    def test_telegram_token_masked_in_summary(self, tmp_path):
        _write_project(
            tmp_path,
            settings_extra="""
telegram:
  bot_token: ${TELEGRAM_BOT_TOKEN:-}
  chat_id: ${TELEGRAM_CHAT_ID:-}
""",
        )
        import os

        os.environ["TELEGRAM_BOT_TOKEN"] = "123456789:AAAdummytelegramtoken"
        os.environ["TELEGRAM_CHAT_ID"] = "424242"
        try:
            cfg = _load(tmp_path, FAKE_ENV)
            summary = cfg.sanitized_summary()
            assert summary["telegram"]["enabled"] is True
            blob = str(summary)
            assert "AAAdummytelegramtoken" not in blob
            assert "424242" not in blob
        finally:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            os.environ.pop("TELEGRAM_CHAT_ID", None)

    def test_mask_db_url(self):
        from config.loader import mask_db_url

        out = mask_db_url("postgresql+psycopg://u:p@h:5432/db")
        assert "p@" not in out.replace("****@", "")
        assert "****" in out
        assert mask_db_url("") == "(disabled)"


class TestProxySection:
    """阶段 8:交易所代理配置段。"""

    def test_proxy_disabled_by_default(self, tmp_path):
        _write_project(tmp_path)
        cfg = _load(tmp_path, FAKE_ENV)
        assert cfg.settings.proxy.enabled is False
        summary = cfg.sanitized_summary()
        assert summary["proxy"]["enabled"] is False
        assert summary["proxy"]["url"] == "(direct)"

    def test_proxy_http_accepted_and_masked(self, tmp_path):
        _write_project(
            tmp_path,
            settings_extra="""
proxy:
  url: ${EXCHANGE_PROXY_URL:-}
""",
        )
        import os

        os.environ["EXCHANGE_PROXY_URL"] = "http://user:secretpw@127.0.0.1:10808"
        try:
            cfg = _load(tmp_path, FAKE_ENV)
            assert cfg.settings.proxy.enabled is True
            summary = cfg.sanitized_summary()
            assert summary["proxy"]["enabled"] is True
            blob = str(summary)
            assert "secretpw" not in blob
            assert "127.0.0.1:10808" in blob  # host:port 可见,凭据不可见
        finally:
            os.environ.pop("EXCHANGE_PROXY_URL", None)

    def test_proxy_bad_scheme_rejected_without_echoing_url(self, tmp_path):
        _write_project(
            tmp_path,
            settings_extra="""
proxy:
  url: ${EXCHANGE_PROXY_URL:-}
""",
        )
        import os

        os.environ["EXCHANGE_PROXY_URL"] = "socks5://hidden-host:1080"
        try:
            with pytest.raises(ConfigError) as ei:
                _load(tmp_path, FAKE_ENV)
            msg = str(ei.value)
            assert "EXCHANGE_PROXY_URL" in msg
            assert "socks5" in msg  # scheme 可见,便于排错
            assert "hidden-host" not in msg  # URL 主体不回显
        finally:
            os.environ.pop("EXCHANGE_PROXY_URL", None)

    def test_mask_proxy_url(self):
        from config.loader import mask_proxy_url

        out = mask_proxy_url("http://user:pw@host:1080")
        assert "pw@" not in out.replace("****@", "")
        assert "user" not in out  # 整段 userinfo 都遮
        assert "host:1080" in out
        assert mask_proxy_url("") == "(direct)"


class TestLiveMode:
    """阶段 8:Live 真实资金模式(项目所有者显式授权;默认仍禁止)。"""

    LIVE_ENV = {
        "TRADING_MODE": "live",
        "ENABLE_LIVE_TRADING": "true",
        "BINANCE_LIVE_API_KEY": "lv-key-abc",
        "BINANCE_LIVE_API_SECRET": "lv-secret-xyz",
    }

    def _with_live_env(self):
        import os

        for k, v in self.LIVE_ENV.items():
            os.environ[k] = v
        return os

    def _pop_live_env(self, os):
        for k in self.LIVE_ENV:
            os.environ.pop(k, None)

    def test_live_requires_enable_flag(self, tmp_path):
        _write_project(tmp_path)
        os = self._with_live_env()
        os.environ.pop("ENABLE_LIVE_TRADING")
        try:
            with pytest.raises(ConfigError) as ei:
                _load(tmp_path, FAKE_ENV)
            assert "ENABLE_LIVE_TRADING" in str(ei.value)
        finally:
            self._pop_live_env(os)

    def test_live_full_opt_in_accepted(self, tmp_path):
        _write_project(tmp_path)
        os = self._with_live_env()
        try:
            cfg = _load(tmp_path, FAKE_ENV)
            assert cfg.trading_mode == "live"
            assert cfg.trading_file.execution.derived_broker == "binance_live_broker"
        finally:
            self._pop_live_env(os)

    def test_live_requires_independent_keys(self, tmp_path):
        _write_project(tmp_path)
        os = self._with_live_env()
        os.environ.pop("BINANCE_LIVE_API_KEY")
        try:
            with pytest.raises(ConfigError) as ei:
                _load(tmp_path, FAKE_ENV)
            msg = str(ei.value)
            assert "BINANCE_LIVE_API_KEY" in msg
            assert "lv-secret" not in msg
        finally:
            self._pop_live_env(os)

    def test_live_requires_risk_enabled(self, tmp_path):
        _write_project(tmp_path)
        os = self._with_live_env()
        os.environ["RISK_ENABLED"] = "false"
        try:
            with pytest.raises(ConfigError) as ei:
                _load(tmp_path, FAKE_ENV)
            assert "risk.enabled" in str(ei.value)
        finally:
            os.environ.pop("RISK_ENABLED", None)
            self._pop_live_env(os)

    def test_live_keys_masked_in_summary(self, tmp_path):
        _write_project(tmp_path)
        os = self._with_live_env()
        try:
            cfg = _load(tmp_path, FAKE_ENV)
            blob = str(cfg.sanitized_summary())
            assert "lv-key-abc" not in blob
            assert "lv-secret-xyz" not in blob
            assert cfg.sanitized_summary()["execution"]["live"]["api_key"] == "****"
        finally:
            self._pop_live_env(os)

    def test_mode_live_without_any_flags_still_default_paper(self, tmp_path):
        """默认(无环境变量)必须仍是 paper——fail-safe 默认不变。"""
        _write_project(tmp_path)
        cfg = _load(tmp_path, FAKE_ENV)
        assert cfg.trading_mode == "paper"
        assert cfg.trading_file.execution.derived_broker == "paper_broker"
