"""日志。密钥在进入任何 handler 之前被抹掉。"""

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "deskpet"
_SECRET_VALUES = set()
_MASK = "***"
_KEY_VALUE = re.compile(
    r"""(?ix)
    ((?<![A-Za-z0-9_])["']?
     (?:password|passwd|pwd|secret|client[_-]?secret|api[_-]?key|apikey|
        authorization|access[_-]?token|refresh[_-]?token|token)
     ["']?\s*[:=]\s*)
    (?:"[^"]*"|'[^']*'|(?:bearer|basic|token)\s+[^\s,;]+|[^\s,;]+)
    """
)
_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{6,}"), False),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"), True),
    (_KEY_VALUE, True),
)


def register_secret(value):
    """登记需要脱敏的密钥值。"""
    text = str(value or "")
    if len(text) >= 6 and text != "local":
        _SECRET_VALUES.add(text)


def redact(text):
    text = str(text)
    for secret in _SECRET_VALUES:
        if secret in text:
            text = text.replace(secret, _MASK)
    for pattern, keep_prefix in _PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if keep_prefix else "") + _MASK, text)
    return text


class _RedactFormatter(logging.Formatter):
    """参数格式化之后再脱敏，覆盖 password=%s 这类写法。"""

    def format(self, record):
        return redact(super().format(record))


def configure(log_dir, level="INFO", dev_mode=False):
    """配置根 logger。重复调用不会叠加 handler。"""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_dir / "deskpet.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(_RedactFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handlers = [file_handler]

    # 打包成窗口程序后没有终端，stderr 是空的，不要再挂控制台 handler。
    if dev_mode and sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(_RedactFormatter("%(levelname)s %(message)s"))
        handlers.append(console)

    for handler in handlers:
        logger.addHandler(handler)

    logger.propagate = False
    return logger


def get_logger(name=None):
    return logging.getLogger(f"{_LOGGER_NAME}.{name}" if name else _LOGGER_NAME)
