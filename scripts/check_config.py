"""配置检查脚本(阶段 0 验收项)。

用法: python scripts/check_config.py
- 全部配置合法:打印脱敏摘要,退出 0;
- 配置缺失/非法:打印只含变量名的错误,退出 1。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ConfigError, load_config  # noqa: E402


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        print("[FAIL] 配置校验未通过:")
        print(str(e))
        return 1
    print("[OK] 配置校验通过。以下为脱敏摘要(不含任何密钥):")
    print(json.dumps(cfg.sanitized_summary(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
