"""FastAPI web app: dashboard + engine control + real-time feed."""
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

# Settings editable live from the UI: name -> (section, coercion type).
EDITABLE: dict[str, tuple[str, type]] = {
    "buy_packs_with_ink": ("engine", bool),
    "auto_open": ("engine", bool),
    "auto_recycle": ("engine", bool),
    "on_empty": ("engine", str),          # "wait" | "stop"
    "recycle_mode": ("engine", str),      # "surplus" | "on_demand"
    "recycle_reserve_mode": ("engine", str),  # "fixed" (keep_spares) | "missing" (per rarity)
    "recycle_priority": ("engine", str),  # "common_first" (preserve rares) | "rare_first" (pure farm)
    "mystery_pack": ("engine", bool),
    "min_ink_reserve": ("engine", int),
    "idle_poll_seconds": ("engine", float),
    "recycle_quota_cooldown_minutes": ("engine", float),  # recycling pause after 429 (quota ~200/day)
    "marketplace_min_interval": ("engine", float),  # guaranteed marketplace cadence (s)
    "autostart": ("engine", bool),
    # Marketplace (commits real cards / other players) — create/cancel contract verified.
    "enabled": ("marketplace", bool),
    "fulfill_others": ("marketplace", bool),
    "max_listings": ("marketplace", int),
    "near_completion_max_missing": ("marketplace", int),  # 0 = all; N = series with <= N missing
    "dry_run": ("marketplace", bool),
}

# Shared objects (initialized at startup via lifespan)
state: dict = {}

# Anti-spam for the lazy sync of /api/live when the engine is stopped.
_live_sync: dict = {"at": 0.0, "busy": False}


def _coerce(typ: type, value):
    if typ is bool:
        return value in (True, "true", "1", 1, "on", "yes")
    return typ(value)


def _editable_view(settings) -> dict:
    return {name: settings.raw[section].get(name) for name, (section, _) in EDITABLE.items()}


def _overridden_keys(db) -> list:
    """Names of settings currently forced by the UI (present in settings_overrides)."""
    ov = db.get_json("settings_overrides", {}) or {}
    return [k for sec in ov.values() if isinstance(sec, dict) for k in sec]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    db = Database(settings.paths["database"])
    # Log level adjusted from the UI (persisted): applied before logging config.
    log_over = db.get_json("logging_override")
    if isinstance(log_over, dict):
        settings.raw["logging"].update(log_over)
    setup_logging(settings.paths["log_file"], settings.logging.get("level", "INFO"),
                  log_requests=bool(settings.logging.get("log_requests", False)))
    log = logging.getLogger("wikitcg")
    # Settings adjusted live from the UI (persisted in DB): they win over the TOML.
    over = db.get_json("settings_overrides")
    if isinstance(over, dict):
        settings.raw = _deep_merge(settings.raw, over)
    # Session cookie updated from the UI (persisted): wins over the TOML.
    cookie_over = db.get_kv("session_cookie_override")
    if cookie_over:
        settings.raw["api"]["session_cookie"] = cookie_over
    extra_over = db.get_kv("extra_cookies_override")
    if extra_over:
        settings.raw["api"]["extra_cookies"] = extra_over
    bus = EventBus()
    client = WikiTCGClient(settings)
    # Automatically persist any session cookie refreshed by the server.
    client.session_sink = lambda tok: db.set_kv("session_cookie_override", tok)
    engine = Engine(client, db, bus, settings)
    state.update(settings=settings, db=db, bus=bus, client=client, engine=engine, log=log)

    if not settings.has_session:
        log.warning("wtcg_session cookie missing: set api.session_cookie in config.toml "
                    "(the UI starts, but API calls will fail with 401).")
    elif not os.environ.get("WIKITCG_SESSION") and not db.get_kv("session_cookie_override"):
        log.info("Security: the cookie is read from config.toml (in clear text). You may prefer the "
                 "WIKITCG_SESSION environment variable and rotate the token regularly.")
    log.info("Server ready at http://%s:%s", settings.server["host"], settings.server["port"])
    if settings.engine.get("autostart") and settings.has_session:
        log.info("autostart enabled: automatically starting the loop.")
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
    """Fields shared by /api/state and /api/live (status, resources, series, config, session)."""
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
    """LIGHT subset of /api/state (without the actions list): for frequent periodic
    refreshes of the dashboard (ink, packs, series, status, session).

    When the engine is STOPPED, lazily refresh the server status (15 s cache) so the
    info stays accurate even without an active loop."""
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
    """Change the log level LIVE (and request tracing), persisted in the database."""
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
    """Update the editable settings LIVE (takes effect on the next iteration)
    and persist them in the database so they survive a restart."""
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
    # Persist ONLY the keys actually changed (the TOML stays the source for the rest).
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
    """Forget the settings changed via the UI and revert to the config.toml values."""
    settings = state["settings"]
    db: Database = state["db"]
    fresh = load_settings()  # TOML + env vars, without the UI overrides
    for name, (section, _) in EDITABLE.items():
        settings.raw[section][name] = fresh.raw[section].get(name)
    db.set_kv("settings_overrides", "")
    state["bus"].publish({"kind": "config", **_editable_view(settings)})
    return JSONResponse({"ok": True, "config": _editable_view(settings)})


@app.post("/api/auth/cookie")
async def set_cookie(payload: dict) -> JSONResponse:
    """Manual session "refresh": paste a fresh wtcg_session cookie (from the browser).
    Immediate effect (no restart) + persistence. This is the renewal mechanism,
    because wikitcg exposes no server-side refresh endpoint."""
    settings = state["settings"]
    client: WikiTCGClient = state["client"]
    engine: Engine = state["engine"]
    db: Database = state["db"]
    session = (payload or {}).get("session_cookie", "").strip()
    extra = (payload or {}).get("extra_cookies", "").strip()
    if not session or session.count(".") < 2:
        return JSONResponse({"ok": False, "error": "Invalid wtcg_session cookie (a JWT is expected)."},
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
    """READ-ONLY marketplace view (my listings + the active market).
    Makes the marketplace visible even when automation is disabled."""
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
    except Exception as exc:  # auth/network: return the state without breaking the UI
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
    # Rarest to most common (descending rank), then by card number; unknowns last.
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
        pass  # client disconnect or server shutdown (Ctrl+C): normal
    except Exception:
        pass
    finally:
        bus.unsubscribe(q)
