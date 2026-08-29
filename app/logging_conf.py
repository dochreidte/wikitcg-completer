"""Logging setup: console + rotating file.

Goals:
  * READABLE output (dated timestamp, level, short logger name, message);
  * guaranteed UTF-8 on BOTH console and file (no more "prÃªt" mojibake on Windows);
  * level configurable from the config ([logging] level = "DEBUG" to see everything);
  * in DEBUG, every API request is traced (method, route, status, duration) — see api_client.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
           "WARNING": logging.WARNING, "WARN": logging.WARNING, "ERROR": logging.ERROR}


def _utf8_stream(stream):
    """Force the console to UTF-8 when possible (avoids cp1252 mojibake on Windows)."""
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # Python >= 3.7
    except (AttributeError, ValueError):
        pass
    return stream


def setup_logging(log_file: str = "wikitcg.log", level: str | int = logging.INFO,
                  *, log_requests: bool = False) -> logging.Logger:
    if isinstance(level, str):
        level = _LEVELS.get(level.upper(), logging.INFO)

    logger = logging.getLogger("wikitcg")
    logger.setLevel(level)
    if not logger.handlers:                 # first call: install the handlers
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

    # Fine-grained request tracing: API client in DEBUG on demand, otherwise it inherits
    # from the parent. (Safe to re-call at runtime to change the level from the UI.)
    logging.getLogger("wikitcg.api").setLevel(logging.DEBUG if log_requests else logging.NOTSET)
    return logger
