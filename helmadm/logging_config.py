from __future__ import annotations

import logging
import sys
from typing import Any

PACKAGE_LOGGER = "helmadm"

_LOG_DATE_FORMAT = "%H:%M:%S"
_LEVEL_WIDTH = 5
_LOGGER_WIDTH = 30


class AlignedFormatter(logging.Formatter):
    """Pad level and logger name so log messages start in the same column."""

    def format(self, record: logging.LogRecord) -> str:
        record.levelname_padded = record.levelname.ljust(_LEVEL_WIDTH)
        record.name_padded = record.name.ljust(_LOGGER_WIDTH)
        return super().format(record)


def setup_logging(*, verbose: bool = False) -> None:
    """Configure package logging; enable DEBUG on stderr when verbose is set."""
    level = logging.DEBUG if verbose else logging.WARNING
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            AlignedFormatter(
                "%(asctime)s %(levelname_padded)s [%(name_padded)s] %(message)s",
                datefmt=_LOG_DATE_FORMAT,
            )
        )
        logger.addHandler(handler)

    logger.propagate = False

    for noisy in ("kubernetes", "urllib3", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the package namespace."""
    if name.startswith(f"{PACKAGE_LOGGER}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{PACKAGE_LOGGER}.{name}")


def trace_values(logger: logging.Logger, msg: str, *args: Any) -> None:
    """Emit values/diff debug lines only when HELMADM_TRACE_VALUES is set."""
    from helmadm.env import trace_values_enabled

    if trace_values_enabled():
        logger.debug(msg, *args)
