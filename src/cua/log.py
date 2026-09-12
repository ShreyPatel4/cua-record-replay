"""Structured logging: JSON lines on stderr through structlog, configured once per process.

Call configure_logging at CLI entry; modules take loggers from structlog.get_logger().
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    levels = logging.getLevelNamesMapping()
    try:
        numeric = levels[level.upper()]
    except KeyError as exc:
        raise ValueError(f"unknown log level {level!r}") from exc
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
