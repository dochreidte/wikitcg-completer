"""Multi-account entry point: sequential farming of one series per account.

Usage:  python run_multi.py  [path/to/accounts.toml]

Reads the global config (config.toml) for shared settings (throttle, retry, recycling...)
then accounts.toml for the account list. Each account opens ITS series and recycles;
we switch to the next account as soon as it has nothing left to do. Ctrl+C to stop.
"""
from __future__ import annotations

import asyncio
import sys

from app.config import load_settings
from app.logging_conf import setup_logging
from app.orchestrator import run_from_config


def main() -> None:
    settings = load_settings()
    setup_logging(settings.paths["log_file"], settings.logging.get("level", "INFO"),
                  log_requests=bool(settings.logging.get("log_requests", False)))
    accounts_path = sys.argv[1] if len(sys.argv) > 1 else "accounts.toml"
    try:
        asyncio.run(run_from_config(accounts_path))
    except KeyboardInterrupt:
        print("\nShutdown requested (Ctrl+C).")


if __name__ == "__main__":
    main()
