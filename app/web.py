"""Application web FastAPI : tableau de bord + contrôle du moteur + flux temps réel."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from . import strategy
from .api_client import WikiTCGClient, AuthError
from .auth import token_status
from .config import load_settings, _deep_merge
from .db import Database
from .engine import Engine
from .events import EventBus
from .logging_conf import setup_logging

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"

# Réglages modifiables en direct depuis l'UI : nom -> (section, type de coercition).
EDITABLE: dict[str, tuple[str, type]] = {
    "buy_packs_with_ink": ("engine", bool),
    "auto_open": ("engine", bool),
    "auto_recycle": ("engine", bool),
    "on_empty": ("engine", str),          # "wait" | "stop"
    "recycle_mode": ("engine", str),      # "surplus" | "on_demand"
    "recycle_reserve_mode": ("engine", str),  # "fixed" (keep_spares) | "missing" (par rareté)
    "recycle_priority": ("engine", str),  # "common_first" (préserve les rares) | "rare_first" (farm pur)
    "mystery_pack": ("engine", bool),
    "min_ink_reserve": ("engine", int),
    "idle_poll_seconds": ("engine", float),
    "recycle_quota_cooldown_minutes": ("engine", float),  # pause recyclage après 429 (quota ~200/j)
    "marketplace_min_interval": ("engine", float),  # cadence marketplace garantie (s)
    "autostart": ("engine", bool),
    # Marketplace (engage de vraies cartes / d'autres joueurs) — contrat create/cancel vérifié.
    "enabled": ("marketplace", bool),
    "fulfill_others": ("marketplace", bool),
    "max_listings": ("marketplace", int),
    "near_completion_max_missing": ("marketplace", int),  # 0 = toutes ; N = séries ≤ N manquantes
    "dry_run": ("marketplace", bool),
}

# Objets partagés (initialisés au démarrage via lifespan)
state: dict = {}

# Anti-spam du sync paresseux de /api/live quand le moteur est à l'arrêt.
_live_sync: dict = {"at": 0.0, "busy": False}


def _coerce(typ: type, value):
    if typ is bool:
        return value in (True, "true", "1", 1, "on", "yes")
    return typ(value)


def _editable_view(settings) -> dict:
    return {name: settings.raw[section].get(name) for name, (section, _) in EDITABLE.items()}


def _overridden_keys(db) -> list:
    """Noms des réglages actuellement forcés par l'UI (présents dans settings_overrides)."""
    ov = db.get_json("settings_overrides", {}) or {}
    return [k for sec in ov.values() if isinstance(sec, dict) for k in sec]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    db = Database(settings.paths["database"])
    # Niveau de log ajusté depuis l'UI (persisté) : appliqué avant la config des logs.
    log_over = db.get_json("logging_override")
    if isinstance(log_over, dict):
        settings.raw["logging"].update(log_over)
    setup_logging(settings.paths["log_file"], settings.logging.get("level", "INFO"),
                  log_requests=bool(settings.logging.get("log_requests", False)))
    log = logging.getLogger("wikitcg")
    # Réglages ajustés en direct depuis l'UI (persistés en base) : ils priment sur le TOML.
    over = db.get_json("settings_overrides")
    if isinstance(over, dict):
        settings.raw = _deep_merge(settings.raw, over)
    # Cookie de session mis à jour depuis l'UI (persisté) : prime sur le TOML.
    cookie_over = db.get_kv("session_cookie_override")
    if cookie_over:
        settings.raw["api"]["session_cookie"] = cookie_over
    extra_over = db.get_kv("extra_cookies_override")
    if extra_over:
        settings.raw["api"]["extra_cookies"] = extra_over
    bus = EventBus()
    client = WikiTCGClient(settings)
    # Persiste automatiquement tout cookie de session rafraîchi par le serveur.
    client.session_sink = lambda tok: db.set_kv("session_cookie_override", tok)
    engine = Engine(client, db, bus, settings)
    state.update(settings=settings, db=db, bus=bus, client=client, engine=engine, log=log)

    if not settings.has_session:
        log.warning("Cookie wtcg_session absent : renseigne api.session_cookie dans config.toml "
                    "(l'UI démarre, mais les appels API échoueront en 401).")
    elif not os.environ.get("WIKITCG_SESSION") and not db.get_kv("session_cookie_override"):
        log.info("Sécurité : le cookie est lu depuis config.toml (en clair). Tu peux préférer la "
                 "variable d'environnement WIKITCG_SESSION et faire tourner le token régulièrement.")
    log.info("Serveur prêt sur http://%s:%s", settings.server["host"], settings.server["port"])
    if settings.engine.get("autostart") and settings.has_session:
        log.info("autostart activé : démarrage automatique de la boucle.")
        engine.start()
    try:
        yield
    finally:
        if engine.running:
            await engine.stop()
        await client.aclose()
        db.close()


app = FastAPI(title="wikitcg-completer", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return FRONTEND.read_text(encoding="utf-8")


def _live_payload(engine, db, settings) -> dict:
    """Champs partagés par /api/state et /api/live (statut, ressources, séries, config, session)."""
    return {
        "status": engine.status,
        "running": engine.running,
        "has_session": settings.has_session,
        "recycle_enabled": bool(settings.recycle.get("duplicates_endpoint")),
        "resources": engine.resources,
        "series": db.progress_view(),
        "config": _editable_view(settings),
        "config_overridden": _overridden_keys(db),
        "logging": dict(settings.logging),
        "token": token_status(state["client"].session_cookie),
        "auth_error": engine.auth_error,
    }


@app.get("/api/state")
async def get_state() -> JSONResponse:
    engine: Engine = state["engine"]
    db: Database = state["db"]
    settings = state["settings"]
    return JSONResponse({
        **_live_payload(engine, db, settings),
        "actions": db.recent_actions(80),
        "empirical_rates": db.empirical_pull_rates(),
        "recycle_values": db.empirical_recycle_values(),
        "full_restock_ink": settings.packs.get("full_restock_ink", 400),
    })


@app.get("/api/live")
async def get_live() -> JSONResponse:
    """Sous-ensemble LÉGER de /api/state (sans la liste d'actions) : pour un rafraîchissement
    périodique fréquent du tableau de bord (encre, packs, séries, statut, session).

    Quand le moteur est À L'ARRÊT, on rafraîchit paresseusement le statut serveur (cache 15 s)
    pour que les infos restent justes même sans boucle active."""
    engine: Engine = state["engine"]
    db: Database = state["db"]
    settings = state["settings"]
    if (not engine.running and engine.status != "syncing"
            and settings.has_session and not _live_sync["busy"]):
        if time.monotonic() - _live_sync["at"] >= 15:
            _live_sync["busy"] = True
            try:
                await engine.sync_status()
            except AuthError as exc:
                engine.auth_error = str(exc)
            except Exception:
                pass
            finally:
                _live_sync["at"] = time.monotonic()
                _live_sync["busy"] = False
    return JSONResponse(_live_payload(engine, db, settings))


@app.post("/api/logging")
async def set_logging(payload: dict) -> JSONResponse:
    """Change le niveau de log À CHAUD (et la trace des requêtes), persisté en base."""
    settings = state["settings"]
    db: Database = state["db"]
    lvl = str((payload or {}).get("level", settings.logging.get("level", "INFO"))).upper()
    if lvl not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        lvl = "INFO"
    reqs = bool((payload or {}).get("log_requests", settings.logging.get("log_requests", False)))
    settings.raw["logging"]["level"] = lvl
    settings.raw["logging"]["log_requests"] = reqs
    setup_logging(settings.paths["log_file"], lvl, log_requests=reqs)
    db.set_json("logging_override", {"level": lvl, "log_requests": reqs})
    state["bus"].publish({"kind": "logging", **settings.logging})
    return JSONResponse({"ok": True, "logging": dict(settings.logging)})


@app.get("/api/config")
async def get_config() -> JSONResponse:
    return JSONResponse(_editable_view(state["settings"]))


@app.post("/api/config")
async def set_config(payload: dict) -> JSONResponse:
    """Met à jour les réglages éditables EN DIRECT (effet dès l'itération suivante)
    et les persiste en base pour survivre à un redémarrage."""
    settings = state["settings"]
    db: Database = state["db"]
    applied: dict = {}
    for name, value in (payload or {}).items():
        spec = EDITABLE.get(name)
        if not spec:
            continue
        section, typ = spec
        try:
            settings.raw[section][name] = _coerce(typ, value)
            applied[name] = settings.raw[section][name]
        except (TypeError, ValueError):
            continue
    # Ne persiste QUE les clés réellement modifiées (le TOML reste la source pour le reste).
    existing = db.get_json("settings_overrides", {})
    if not isinstance(existing, dict):
        existing = {}
    for name in applied:
        section = EDITABLE[name][0]
        existing.setdefault(section, {})[name] = settings.raw[section][name]
    db.set_json("settings_overrides", existing)
    state["bus"].publish({"kind": "config", **_editable_view(settings)})
    return JSONResponse({"ok": True, "applied": applied, "config": _editable_view(settings)})


@app.post("/api/config/reset")
async def reset_config() -> JSONResponse:
    """Oublie les réglages modifiés via l'UI et revient aux valeurs de config.toml."""
    settings = state["settings"]
    db: Database = state["db"]
    fresh = load_settings()  # TOML + variables d'env, sans les overrides UI
    for name, (section, _) in EDITABLE.items():
        settings.raw[section][name] = fresh.raw[section].get(name)
    db.set_kv("settings_overrides", "")
    state["bus"].publish({"kind": "config", **_editable_view(settings)})
    return JSONResponse({"ok": True, "config": _editable_view(settings)})


@app.post("/api/auth/cookie")
async def set_cookie(payload: dict) -> JSONResponse:
    """« Refresh » manuel de session : on colle un cookie wtcg_session frais (du navigateur).
    Effet immédiat (sans redémarrage) + persistance. C'est le mécanisme de renouvellement,
    car wikitcg n'expose aucun endpoint de refresh côté serveur."""
    settings = state["settings"]
    client: WikiTCGClient = state["client"]
    engine: Engine = state["engine"]
    db: Database = state["db"]
    session = (payload or {}).get("session_cookie", "").strip()
    extra = (payload or {}).get("extra_cookies", "").strip()
    if not session or session.count(".") < 2:
        return JSONResponse({"ok": False, "error": "Cookie wtcg_session invalide (un JWT est attendu)."},
                            status_code=400)
    client.update_session(session, extra)
    settings.raw["api"]["session_cookie"] = session
    db.set_kv("session_cookie_override", session)
    if extra:
        settings.raw["api"]["extra_cookies"] = extra
        db.set_kv("extra_cookies_override", extra)
    engine.auth_error = None
    ts = token_status(session)
    state["bus"].publish({"kind": "token", **ts})
    return JSONResponse({"ok": True, "token": ts})


@app.get("/api/marketplace")
async def marketplace() -> JSONResponse:
    """Vue marketplace EN LECTURE SEULE (mes annonces + le marché actif).
    Sert à rendre la marketplace visible même quand l'automatisation est désactivée."""
    settings = state["settings"]
    client: WikiTCGClient = state["client"]
    cfg = settings.marketplace
    fields = ("id", "status", "offered_card_type", "offered_series", "offered_rarity",
              "wanted_card_id", "wanted_series_id", "lister_name", "expires_at")

    def norm(l: dict) -> dict:
        return {k: l.get(k) for k in fields}

    try:
        mine = await client.marketplace_mine()
        browse = await client.marketplace_browse()
    except Exception as exc:  # auth/réseau : on renvoie l'état sans casser l'UI
        return JSONResponse({"enabled": bool(cfg.get("enabled")), "error": str(exc),
                             "mine": [], "market": []})
    mine_l = [norm(l) for l in (mine.get("listings") or [])]
    market = [norm(l) for l in (browse.get("listings") or []) if l.get("status") == "active"]
    return JSONResponse({
        "enabled": bool(cfg.get("enabled")),
        "max_listings": cfg.get("max_listings", 5),
        "active_mine": sum(1 for l in mine_l if l["status"] == "active"),
        "mine": mine_l,
        "market": market,
    })


@app.get("/api/stats")
async def get_stats() -> JSONResponse:
    db: Database = state["db"]
    return JSONResponse({
        "history": db.resource_history(120),
        "pull_counts": db.pull_counts(),
        "recycle": db.recycle_summary(),
        "recycle_values": db.empirical_recycle_values(),
        "recycle_failures": db.recycle_failures_summary(),
    })


@app.get("/api/series/{sid}")
async def series_detail(sid: str) -> JSONResponse:
    db: Database = state["db"]
    prog = next((p for p in db.progress_view() if p["series_id"] == sid), None)
    rarity = db.rarity_breakdown(sid)
    missing = db.missing_cards(sid)
    # Du plus rare au plus commun (rang décroissant), puis par numéro de carte ; inconnues en dernier.
    missing.sort(key=lambda m: (-strategy.RARITY_RANK.get(m["rarity"], -1), m.get("card_number") or 0))
    return JSONResponse({
        "series": prog,
        "rarity": rarity,
        "missing": missing,
        "catalog_loaded": db.catalog_size(sid) > 0,
        "duplicates": len(db.duplicates(sid)),
    })


@app.post("/api/control/start")
async def control_start() -> dict:
    state["engine"].start()
    return {"ok": True, "status": state["engine"].status}


@app.post("/api/control/stop")
async def control_stop() -> dict:
    await state["engine"].stop()
    return {"ok": True, "status": state["engine"].status}


@app.post("/api/control/sync")
async def control_sync() -> dict:
    asyncio.create_task(state["engine"].sync_now())
    return {"ok": True}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    bus: EventBus = state["bus"]
    q = bus.subscribe()
    try:
        # snapshot initial
        await websocket.send_json({"kind": "status",
                                   "status": state["engine"].status,
                                   "running": state["engine"].running})
        while True:
            event = await q.get()
            await websocket.send_json(event)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass  # déconnexion client ou arrêt serveur (Ctrl+C) : normal
    except Exception:
        pass
    finally:
        bus.unsubscribe(q)
