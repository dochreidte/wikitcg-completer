"""Point d'entrée : lance le serveur web.  Usage : python run.py"""
from __future__ import annotations

import uvicorn

from app.config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run("app.web:app", host=settings.server["host"],
                port=int(settings.server["port"]), reload=False, log_level="warning")


if __name__ == "__main__":
    main()
