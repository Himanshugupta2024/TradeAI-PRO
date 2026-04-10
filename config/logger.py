"""
Centralized Logging Configuration.

Usage in any module:
    from config.logger import get_logger
    log = get_logger(__name__)
    log.info("Fetched 200 candles", extra={"symbol": "BTC/USDT"})

Outputs:
  - Console: coloured, concise
  - File: detailed JSON lines → logs/trading.log (rotated at 5MB, 5 backups)
"""

import os
import sys
import json
import logging
import logging.handlers
import threading
from datetime import datetime, timezone


LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "trading.log")
ERROR_LOG = os.path.join(LOG_DIR, "errors.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

_HANDLER_LOCK = threading.Lock()
_SHARED_HANDLERS: tuple[logging.Handler, ...] | None = None


class JsonFormatter(logging.Formatter):
    """Structured JSON log formatter for file output."""

    def format(self, record):
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
            "msg": record.getMessage(),
        }
        # Merge any extra fields
        if hasattr(record, "symbol"):
            entry["symbol"] = record.symbol
        if hasattr(record, "trade_id"):
            entry["trade_id"] = record.trade_id
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])
        return json.dumps(entry)


class ConsoleFormatter(logging.Formatter):
    """Coloured, concise console output."""

    COLORS = {
        "DEBUG": "\033[90m",     # grey
        "INFO": "\033[36m",      # cyan
        "WARNING": "\033[33m",   # yellow
        "ERROR": "\033[31m",     # red
        "CRITICAL": "\033[1;31m",# bold red
    }
    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelname, "")
        ts = datetime.now().strftime("%H:%M:%S")
        module = record.module[:12].ljust(12)
        return f"{color}{ts} {record.levelname[0]} {module} {record.getMessage()}{self.RESET}"


def _build_shared_handlers() -> tuple[logging.Handler, ...]:
    """Create shared handlers once to avoid rotating the same file from many handlers."""
    os.makedirs(LOG_DIR, exist_ok=True)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(ConsoleFormatter())

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonFormatter())

    error_handler = logging.handlers.RotatingFileHandler(
        ERROR_LOG, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(JsonFormatter())

    return (console, file_handler, error_handler)


def _get_shared_handlers() -> tuple[logging.Handler, ...]:
    """Return process-wide shared handlers, initializing once in a thread-safe way."""
    global _SHARED_HANDLERS

    if _SHARED_HANDLERS is not None:
        return _SHARED_HANDLERS

    with _HANDLER_LOCK:
        if _SHARED_HANDLERS is None:
            _SHARED_HANDLERS = _build_shared_handlers()

    return _SHARED_HANDLERS


def get_logger(name: str) -> logging.Logger:
    """
    Get a configured logger.

    Args:
        name: Usually __name__ of the calling module.

    Returns:
        logging.Logger with console + file handlers.
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    logger.propagate = False

    for handler in _get_shared_handlers():
        if handler not in logger.handlers:
            logger.addHandler(handler)

    return logger
