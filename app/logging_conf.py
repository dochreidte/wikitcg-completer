"""Logging setup: console + rotating file with UTF-8 encoding."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
           "WARNING": logging.WARNING, "WARN": logging.WARNING, "ERROR": logging.ERROR}


def _utf8_stream(stream):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    return stream


def setup_logging(log_file: str = "wikitcg.log", level: str | int = logging.INFO,
                  *, log_requests: bool = False) -> logging.Logger:
    if isinstance(level, str):
        level = _LEVELS.get(level.upper(), logging.INFO)

    logger = logging.getLogger("wikitcg")
    logger.setLevel(level)
    if not logger.handlers:
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)-16s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console = logging.StreamHandler(_utf8_stream(sys.stdout))
        console.setFormatter(fmt)
        logger.addHandler(console)
        fileh = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        fileh.setFormatter(fmt)
        logger.addHandler(fileh)
        logger.propagate = False

    logging.getLogger("wikitcg.api").setLevel(logging.DEBUG if log_requests else logging.NOTSET)
    return logger
