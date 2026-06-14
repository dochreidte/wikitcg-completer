"""Point d'entrée multi-comptes : farm séquentiel d'une série par compte.

Usage :  python run_multi.py  [chemin/vers/accounts.toml]

Lit la config globale (config.toml) pour les réglages communs (throttle, retry, recyclage…)
puis accounts.toml pour la liste des comptes. Chaque compte ouvre SA série et recycle ;
on bascule au compte suivant dès qu'il n'a plus rien à faire. Ctrl+C pour arrêter.
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
        print("\nArrêt demandé (Ctrl+C).")


if __name__ == "__main__":
    main()
