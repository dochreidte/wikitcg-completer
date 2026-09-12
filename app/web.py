"""FastAPI dashboard: accounts, farm control, settings and real-time feed."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import strategy, vault
from .api_client import AuthError
from .auth import token_status
from .config import _deep_merge, load_settings
from .db import Database
from .events import EventBus
from .farm import Farm
from .logging_conf import setup_logging
from .netguard import install, request_allowed

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
FRONTEND = FRONTEND_DIR / "index.html"

EDITABLE: dict[str, tuple[str, type]] = {
    "buy_packs_with_ink": ("engine", bool),
    "auto_recycle": ("engine", bool),
    "mystery_pack": ("engine", bool),
    "enabled": ("marketplace", bool),
    "recycle_mode": ("engine", str),
    "recycle_reserve_mode": ("engine", str),
    "recycle_priority": ("engine", str),
    "min_ink_reserve": ("engine", int),
    "autostart": ("engine", bool),
    "fulfill_others": ("marketplace", bool),
    "max_listings": ("marketplace", int),
    "near_completion_max_missing": ("marketplace", int),
}
CHOICES = {
    "recycle_mode": ("surplus", "on_demand"),
    "recycle_reserve_mode": ("fixed", "missing"),
    "recycle_priority": ("common_first", "rare_first"),
}
MAX_COOKIE = 8192

state: dict = {}
_live_sync: dict = {"at": 0.0, "busy": False}
_background: set[asyncio.Task] = set()


def _coerce(name: str, value):
    typ = EDITABLE[name][1]
    if typ is bool:
        return value in (True, "true", "1", 1, "on", "yes")
    out = typ(value)
    if name in CHOICES and out not in CHOICES[name]:
        raise ValueError(name)
    if typ is int and out < 0:
        raise ValueError(name)
    return out


def _editable_view(settings) -> dict:
    return {name: settings.raw[section].get(name) for name, (section, _) in EDITABLE.items()}


def _overridden_keys(db: Database) -> list:
    ov = db.get_json("settings_overrides", {}) or {}
    if not isinstance(ov, dict):
        return []
    return [k for sec in ov.values() if isinstance(sec, dict) for k in sec]


def _apply_overrides(settings, db: Database) -> None:
    over = db.get_json("settings_overrides")
    if not isinstance(over, dict):
        return
    clean = {sec: {k: v for k, v in vals.items() if EDITABLE.get(k, ("",))[0] == sec}
             for sec, vals in over.items() if isinstance(vals, dict)}
    settings.raw = _deep_merge(settings.raw, clean)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _public(acc: dict) -> dict:
    return {k: acc[k] for k in ("id", "name", "series")}


def _farm() -> Farm:
    return state["farm"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app_db = Database(settings.paths["database"])
    log_over = app_db.get_json("logging_override")
    if isinstance(log_over, dict):
        settings.raw["logging"].update({k: log_over[k] for k in ("level", "log_requests") if k in log_over})
    setup_logging(settings.paths["log_file"], settings.logging["level"],
                  log_requests=bool(settings.logging["log_requests"]))
    log = logging.getLogger("wikitcg")
    _apply_overrides(settings, app_db)
    bus = EventBus()
    farm = Farm(app_db, settings, bus)
    state.update(settings=settings, app_db=app_db, bus=bus, farm=farm)
    await farm.restore()
    if not vault.available():
        log.warning("No secure OS keyring found: set WIKITCG_SESSION_<ACCOUNT_ID> environment variables.")
    log.info("Server ready at http://%s:%s", settings.server["host"], settings.server["port"])
    if settings.engine.get("autostart"):
        farm.start()
    try:
        yield
    finally:
        await farm.close()
        app_db.close()


def _bind_host() -> str | None:
    return state["settings"].server.get("host") if "settings" in state else None


app = FastAPI(title="wikitcg-completer", lifespan=lifespan)
install(app, _bind_host)
app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return FRONTEND.read_text(encoding="utf-8")


def _live_payload() -> dict:
    settings = state["settings"]
    farm = _farm()
    s = farm.session
    payload = {
        "running": farm.running,
        "config": _editable_view(settings),
        "config_overridden": _overridden_keys(state["app_db"]),
        "logging": dict(settings.logging),
        "recycle_enabled": bool(settings.recycle.get("duplicates_endpoint")),
    }
    if s is None:
        return {**payload, "status": "idle", "has_session": False, "resources": {}, "series": [],
                "token": token_status(""), "auth_error": None, "account": None}
    return {**payload, "status": s.engine.status, "has_session": s.settings.has_session,
            "resources": s.engine.resources, "series": s.db.progress_view(),
            "token": token_status(s.client.session_cookie), "auth_error": s.engine.auth_error,
            "account": _public(s.account)}


@app.get("/api/state")
async def get_state() -> JSONResponse:
    s = _farm().session
    history = ({"actions": s.db.recent_actions(80), "empirical_rates": s.db.empirical_pull_rates(),
                "recycle_values": s.db.empirical_recycle_values()} if s
               else {"actions": [], "empirical_rates": {}, "recycle_values": {}})
    return JSONResponse({**_live_payload(), **history,
                         "full_restock_ink": state["settings"].packs.get("full_restock_ink", 400)})


@app.get("/api/live")
async def get_live() -> JSONResponse:
    farm = _farm()
    s = farm.session
    if (s and not farm.running and s.engine.status != "syncing" and s.settings.has_session
            and not _live_sync["busy"] and time.monotonic() - _live_sync["at"] >= 15):
        _live_sync["busy"] = True
        try:
            await s.engine.sync_status()
        except AuthError as exc:
            s.engine.auth_error = str(exc)
        except Exception:
            pass
        finally:
            _live_sync["at"] = time.monotonic()
            _live_sync["busy"] = False
    return JSONResponse(_live_payload())


@app.post("/api/logging")
async def set_logging(payload: dict) -> JSONResponse:
    settings = state["settings"]
    lvl = str(payload.get("level", settings.logging.get("level", "INFO"))).upper()
    if lvl not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        lvl = "INFO"
    reqs = bool(payload.get("log_requests", settings.logging.get("log_requests", False)))
    settings.raw["logging"].update(level=lvl, log_requests=reqs)
    setup_logging(settings.paths["log_file"], lvl, log_requests=reqs)
    state["app_db"].set_json("logging_override", {"level": lvl, "log_requests": reqs})
    state["bus"].publish({"kind": "logging", **settings.logging})
    return JSONResponse({"ok": True, "logging": dict(settings.logging)})


@app.get("/api/config")
async def get_config() -> JSONResponse:
    return JSONResponse(_editable_view(state["settings"]))


@app.post("/api/config")
async def set_config(payload: dict) -> JSONResponse:
    settings = state["settings"]
    db: Database = state["app_db"]
    applied: dict = {}
    for name, value in payload.items():
        if name not in EDITABLE:
            continue
        try:
            applied[name] = _coerce(name, value)
        except (TypeError, ValueError):
            continue
        settings.raw[EDITABLE[name][0]][name] = applied[name]
    existing = db.get_json("settings_overrides", {})
    if not isinstance(existing, dict):
        existing = {}
    for name, value in applied.items():
        existing.setdefault(EDITABLE[name][0], {})[name] = value
    db.set_json("settings_overrides", existing)
    _farm().refresh_settings()
    state["bus"].publish({"kind": "config", **_editable_view(settings)})
    return JSONResponse({"ok": True, "applied": applied, "config": _editable_view(settings)})


@app.post("/api/config/reset")
async def reset_config() -> JSONResponse:
    settings = state["settings"]
    fresh = load_settings()
    for name, (section, _) in EDITABLE.items():
        settings.raw[section][name] = fresh.raw[section].get(name)
    state["app_db"].set_kv("settings_overrides", "")
    _farm().refresh_settings()
    state["bus"].publish({"kind": "config", **_editable_view(settings)})
    return JSONResponse({"ok": True, "config": _editable_view(settings)})


@app.get("/api/accounts")
async def list_accounts() -> JSONResponse:
    return JSONResponse(_farm().snapshot())


@app.post("/api/accounts")
async def add_account(payload: dict) -> JSONResponse:
    try:
        acc = await _farm().add(str(payload.get("name") or ""), str(payload.get("series") or ""))
    except ValueError as exc:
        return _error(str(exc), 400)
    return JSONResponse({"ok": True, "account": _public(acc)})


@app.post("/api/accounts/{account_id}")
async def update_account(account_id: str, payload: dict) -> JSONResponse:
    fields = {k: None if payload.get(k) is None else str(payload[k]) for k in ("name", "series")}
    try:
        acc = await _farm().update(account_id, **fields)
    except ValueError as exc:
        return _error(str(exc), 400)
    if acc is None:
        return _error("Unknown account.", 404)
    return JSONResponse({"ok": True, "account": _public(acc)})


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str) -> JSONResponse:
    try:
        removed = await _farm().remove(account_id)
    except ValueError as exc:
        return _error(str(exc), 400)
    return JSONResponse({"ok": True}) if removed else _error("Unknown account.", 404)


@app.post("/api/accounts/{account_id}/cookie")
async def set_cookie(account_id: str, payload: dict) -> JSONResponse:
    session = str(payload.get("session_cookie") or "").strip()
    extra = str(payload.get("extra_cookies") or "").strip()
    if len(session) > MAX_COOKIE or len(extra) > MAX_COOKIE:
        return _error("Cookie too long.", 400)
    try:
        status = _farm().set_cookie(account_id, session, extra)
    except KeyError:
        return _error("Unknown account.", 404)
    except ValueError as exc:
        return _error(str(exc), 400)
    except vault.VaultError as exc:
        return _error(str(exc), 503)
    return JSONResponse({"ok": True, "token": status})


@app.post("/api/accounts/{account_id}/activate")
async def activate_account(account_id: str) -> JSONResponse:
    try:
        await _farm().activate(account_id)
    except KeyError:
        return _error("Unknown account.", 404)
    return JSONResponse({"ok": True, "current": account_id})


@app.get("/api/marketplace")
async def marketplace() -> JSONResponse:
    cfg = state["settings"].marketplace
    s = _farm().session
    if s is None:
        return JSONResponse({"enabled": bool(cfg.get("enabled")), "error": "No account.",
                             "mine": [], "market": []})
    fields = ("id", "status", "offered_card_type", "offered_series", "offered_rarity",
              "wanted_card_id", "wanted_series_id", "lister_name", "expires_at")
    try:
        mine = await s.client.marketplace_mine()
        browse = await s.client.marketplace_browse()
    except Exception as exc:
        return JSONResponse({"enabled": bool(cfg.get("enabled")), "error": str(exc),
                             "mine": [], "market": []})
    mine_l = [{k: l.get(k) for k in fields} for l in (mine.get("listings") or [])]
    market = [{k: l.get(k) for k in fields} for l in (browse.get("listings") or [])
              if l.get("status") == "active"]
    return JSONResponse({
        "enabled": bool(cfg.get("enabled")),
        "max_listings": cfg.get("max_listings", 5),
        "active_mine": sum(1 for l in mine_l if l["status"] == "active"),
        "mine": mine_l,
        "market": market,
    })


@app.get("/api/stats")
async def get_stats() -> JSONResponse:
    s = _farm().session
    if s is None:
        return JSONResponse({"history": [], "pull_counts": [], "recycle": {},
                             "recycle_values": {}, "recycle_failures": {}})
    return JSONResponse({
        "history": s.db.resource_history(120),
        "pull_counts": s.db.pull_counts(),
        "recycle": s.db.recycle_summary(),
        "recycle_values": s.db.empirical_recycle_values(),
        "recycle_failures": s.db.recycle_failures_summary(),
    })


@app.get("/api/series/{sid}")
async def series_detail(sid: str) -> JSONResponse:
    s = _farm().session
    if s is None:
        return _error("No account.", 404)
    missing = s.db.missing_cards(sid)
    missing.sort(key=lambda m: (-strategy.RARITY_RANK.get(m["rarity"], -1), m.get("card_number") or 0))
    return JSONResponse({
        "series": next((p for p in s.db.progress_view() if p["series_id"] == sid), None),
        "rarity": s.db.rarity_breakdown(sid),
        "missing": missing,
        "catalog_loaded": s.db.catalog_size(sid) > 0,
        "duplicates": len(s.db.duplicates(sid)),
    })


@app.post("/api/control/start")
async def control_start() -> JSONResponse:
    _farm().start()
    return JSONResponse({"ok": True, "running": _farm().running})


@app.post("/api/control/stop")
async def control_stop() -> JSONResponse:
    await _farm().stop()
    return JSONResponse({"ok": True, "running": _farm().running})


@app.post("/api/control/next")
async def control_next() -> JSONResponse:
    await _farm().next()
    return JSONResponse({"ok": True, "current": _farm().current_id()})


@app.post("/api/control/sync")
async def control_sync() -> JSONResponse:
    s = _farm().session
    if s is None:
        return _error("No account.", 409)
    task = asyncio.create_task(s.engine.sync_now())
    _background.add(task)
    task.add_done_callback(_background.discard)
    return JSONResponse({"ok": True})


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    if not request_allowed("WEBSOCKET", websocket.headers, _bind_host()):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    bus: EventBus = state["bus"]
    q = bus.subscribe()

    async def pump() -> None:
        try:
            s = _farm().session
            await websocket.send_json({"kind": "status", "status": s.engine.status if s else "idle",
                                       "running": _farm().running})
            while True:
                await websocket.send_json(await q.get())
        except Exception:
            pass

    sender = asyncio.create_task(pump())
    try:
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        sender.cancel()
        bus.unsubscribe(q)
