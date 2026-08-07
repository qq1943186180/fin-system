"""Application logging with rotation and defense-in-depth redaction."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app_paths import LOG_DIR, ensure_app_dirs


LOGGER_NAME = "finance_system"
DEFAULT_LOG_FILE = "finance-system.log"
REDACTED = "[REDACTED]"

_KEYS = (
    "app_secret|client_secret|api[_-]?key|password|authorization|"
    "tenant_access_token|app_access_token|access_token|refresh_token|"
    "invoice_no|invoice_number|seller_tax_id|buyer_tax_id|"
    "发票号码|销方税号|销售方税号|购方税号|购买方税号"
)
_PATTERNS = (
    # Run this before the generic key/value rule so it consumes the complete
    # Authorization value instead of redacting only the word "Bearer".
    re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        rf"(?i)([\"']?(?:{_KEYS})[\"']?\s*[:=]\s*[\"']?)([^\"',\s}}\]]+)"
    ),
)


def redact_text(value: object) -> str:
    """Return a log-safe string with credentials and invoice IDs removed."""

    text = str(value)
    for pattern in _PATTERNS:
        def replace(match: re.Match[str]) -> str:
            # The formatter is intentionally a second redaction layer.  Keep
            # already-redacted key/value fields stable and readable.
            if match.lastindex and match.lastindex >= 2:
                current = match.group(2)
                if current.startswith("[REDACTED"):
                    return match.group(0)
            return match.group(1) + REDACTED

        text = pattern.sub(replace, text)
    return text


class RedactingFilter(logging.Filter):
    """Remove sensitive values before a record reaches any configured handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Also redact rendered exception text and chained tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def configure_logging(
    *,
    level: int | str = logging.INFO,
    log_dir: Path | str | None = None,
    filename: str = DEFAULT_LOG_FILE,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """Configure and return the application logger exactly once per log path."""

    ensure_app_dirs()
    directory = Path(log_dir) if log_dir is not None else LOG_DIR
    directory.mkdir(parents=True, exist_ok=True)
    log_path = (directory / filename).resolve()

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == log_path:
            handler.setLevel(level)
            return logger

    handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,
    )
    handler.setLevel(level)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s [%(process)d:%(threadName)s] %(message)s"
        )
    )
    logger.addHandler(handler)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return an application child logger, configuring the root if necessary."""

    root = configure_logging()
    return root if not name else root.getChild(name)
