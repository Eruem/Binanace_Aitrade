"""结构化日志(阶段 7,指导书 12)。

JSON 行日志,字段至少包含:timestamp、level、cycle_id、thread_id、
decision_id、agent_name、task、model_alias、provider、latency_ms、
result_status、error_code(缺省为 null,由调用方按需 extra 补充)。

脱敏(安全边界):格式化输出前对整行文本执行正则替换,覆盖
API Key(sk-…)、Bearer/Authorization、token/password 键值、
Telegram Bot Token(数字:长串)、URL 内的 user:password 与
数据库连接串口令——无论来自 message 还是 extra 字段。
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

# §12 规定的标准字段(缺省写 null,便于下游按字段聚合)
STANDARD_FIELDS = (
    "timestamp",
    "level",
    "logger",
    "message",
    "cycle_id",
    "thread_id",
    "decision_id",
    "agent_name",
    "task",
    "model_alias",
    "provider",
    "latency_ms",
    "result_status",
    "error_code",
)

# 脱敏正则(在序列化后的整行 JSON 上执行,兜底一切来源)
_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # OpenAI 风格密钥
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "****"),
    # Bearer / Authorization
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-]+"), r"\1 ****"),
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*)[\"']?[^\s,\"'}]+"), r"\1****"),
    # key=value 形式的密钥字段
    (
        re.compile(
            r"(?i)([\"']?(?:api[_-]?key|api[_-]?secret|secret|token|password|passwd)"
            r"[\"']?\s*[:=]\s*)[\"']?[^\s,\"'}]+"
        ),
        r"\1****",
    ),
    # Telegram Bot Token 形如 123456789:AAH...
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{20,}\b"), "****"),
    # URL 内嵌凭据 scheme://user:pass@host
    (re.compile(r"(://[^/@\s:]+):[^@/\s]*@"), r"\1:****@"),
]


def redact(text: str) -> str:
    """对文本执行全部脱敏替换。"""
    for pattern, repl in _REDACT_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactionFilter(logging.Filter):
    """在记录阶段就改写 msg,args,确保 formatter 输出已被脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: redact(str(v)) for k, v in record.args.items()}
                else:
                    record.args = tuple(
                        redact(a) if isinstance(a, str) else a for a in record.args
                    )
        except Exception:  # 脱敏失败也不能让日志系统崩溃
            record.msg = "<redaction failed>"
            record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    """把 LogRecord 输出为一行 JSON(含 §12 全部标准字段)。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            ) + f".{int(record.created % 1 * 1000):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "cycle_id": getattr(record, "cycle_id", None),
            "thread_id": getattr(record, "thread_id", None),
            "decision_id": getattr(record, "decision_id", None),
            "agent_name": getattr(record, "agent_name", None),
            "task": getattr(record, "task", None),
            "model_alias": getattr(record, "model_alias", None),
            "provider": getattr(record, "provider", None),
            "latency_ms": getattr(record, "latency_ms", None),
            "result_status": getattr(record, "result_status", None),
            "error_code": getattr(record, "error_code", None),
        }
        # 允许调用方附加非标准字段(extra=...)
        for key, value in record.__dict__.items():
            if key not in payload and not key.startswith("_") and key not in (
                "args", "exc_info", "exc_text", "stack_info", "pathname",
                "filename", "module", "lineno", "funcName", "created",
                "msecs", "relativeCreated", "thread", "threadName",
                "processName", "process", "taskName", "message", "name",
                "levelname", "levelno",
            ):
                payload[key] = value
        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = redact(
                self.formatException(record.exc_info)[:4000]
            )
        line = json.dumps(payload, ensure_ascii=False, default=str)
        return redact(line)  # 最后一道兜底:整行再脱敏


_configured = False


def setup_logging(
    level: str = "INFO", *, stream: Any = None, file: Any = None
) -> logging.Logger:
    """配置根 logger 为 JSON 行输出(幂等)。返回 app logger。

    file: 可选日志文件路径(同样 JSON 行 + 脱敏)。常驻循环用它写
    `var/agent_loop.log`——监控面板按该文件 mtime 判断循环是否活跃。
    """
    global _configured
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if _configured:
        return logging.getLogger("app")
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    root.addHandler(handler)
    if file is not None:
        from pathlib import Path

        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(RedactionFilter())
        root.addHandler(file_handler)
    # 移除可能存在的旧 handler(测试反复 setup 时避免重复输出)
    _configured = True
    return logging.getLogger("app")


def get_logger(name: str) -> logging.Logger:
    """带 RedactionFilter 的 logger(未 setup 时也保证脱敏)。"""
    logger = logging.getLogger(name)
    if not any(isinstance(f, RedactionFilter) for f in logger.filters):
        logger.addFilter(RedactionFilter())
    return logger


def log_event(
    logger: logging.Logger,
    level: int,
    message: str,
    **fields: Any,
) -> None:
    """按 §12 字段记录一条结构化日志。"""
    logger.log(level, message, extra=fields)
