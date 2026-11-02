"""数据库迁移脚本(阶段 7)。

用法:
    python scripts/migrate_db.py        # 应用 config 中 DATABASE_URL 的未执行迁移
    python scripts/migrate_db.py --status  # 只显示已应用/待应用版本

DATABASE_URL 为空时拒绝运行(未启用数据库不应误建 SQLite 文件);
本地无 PostgreSQL 可在 .env 用 sqlite:///var/agent.db。
错误信息只含变量名/URL scheme,不含口令。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ConfigError, load_config  # noqa: E402
from database.migrations import MIGRATIONS, apply_migrations, applied_versions
from database.models import make_engine


def main() -> int:
    parser = argparse.ArgumentParser(description="应用数据库迁移")
    parser.add_argument("--status", action="store_true", help="只显示状态")
    args = parser.parse_args()

    try:
        cfg = load_config()
    except ConfigError as e:
        print("[FATAL] 配置错误(信息已脱敏):", file=sys.stderr)
        print(str(e), file=sys.stderr)
        return 1

    url = cfg.settings.database.url
    if not url:
        print(
            "DATABASE_URL 未设置,数据库未启用;"
            "请在 .env 配置(参考 .env.example)后重试",
            file=sys.stderr,
        )
        return 1

    engine = make_engine(url)
    try:
        if args.status:
            done = applied_versions(engine)
            print(f"已应用: {sorted(done) or '(无)'}")
            pending = [v for v, _ in MIGRATIONS if v not in done]
            print(f"待应用: {pending or '(无)'}")
            return 0
        fresh = apply_migrations(engine)
        if fresh:
            print(f"已应用迁移: {', '.join(fresh)}")
        else:
            print("schema 已是最新,无需迁移")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
