"""Automation engine (MVP): sync + auto-opening + recycling.

Loop, in "fully automatic" mode:
  1. read the status (ink, free packs, level);
  2. pick the incomplete series missing the most cards (new series are auto-detected
     from /api/series and opened until complete);
  3. open a booster, record the pulls, update the inventory;
  4. recycle the surplus of duplicates (per policy, if the endpoint is configured);
  5. when free packs are exhausted: buy a restock with ink
     (recycling duplicates first if needed); otherwise wait for regeneration.

Marketplace trades remain opt-in (phase 2, disabled by default).
"""
from __future__ import annotations

import asyncio
import logging
import time

from .api_client import ApiError, AuthError, WikiTCGClient
from .db import Database
from .events import EventBus
from .engine_actions import ActionsMixin
from .engine_market import MarketMixin
from .series_seed import SERIES_SEED, DEFAULT_PRIMARY, DEFAULT_ACCENT
from . import strategy

log = logging.getLogger("wikitcg.engine")

_LOG_FN = {"warn": log.warning, "error": log.error}


def _prettify(series_id: str) -> str:
    return series_id.replace("-", " ").replace("_", " ").title()


class Engine(ActionsMixin, MarketMixin):
    def __init__(self, client: WikiTCGClient, db: Database, bus: EventBus, settings):
        self.client = client
        self.db = db
        self.bus = bus
        self.settings = settings
        self.running = False
        self.status = "idle"
        self.resources: dict = {}
        self._task: asyncio.Task | None = None
        self._recycle_warned = False
        self._opens_since_resync = 0
        self.opened_total = 0
        self.recycled_total = 0
        self._last_target = None
        self.auth_error: str | None = None
        self._my_listings: list[dict] = []
        # pullIds that returned 500 (e.g. locked in a deck) -> epoch before which we don't retry.
        self._recycle_skip: dict[str, float] = self.db.recycle_skips()
        # /api/cards/duplicates lags behind: never resubmit a pullId already handled this session.
        self._recycle_done: set[str] = set()
        self._mystery_due_at = float(self.db.get_kv("mystery_due_at", "0") or 0)
        # Daily recycle quota hit (429): epoch until which recycling is suspended.
        self._recycle_quota_until = float(self.db.get_kv("recycle_quota_until", "0") or 0)
        self._idle_backoff = 0.0
        self._mkt_backoff = 0.0
        self._last_mkt_pass = 0.0
        self._last_full_sync = 0.0
        self._last_series_check = 0.0
        # Series from /api/series (openable packs; mystery is not listed). None until first read.
        self._openable: set[str] | None = None
        # Set when ink/packs become available so the idle opener wakes immediately.
        self._wake = asyncio.Event()

    def _emit(self, type_: str, message: str, *, series_id: str = "",
              level: str = "info", detail: dict | None = None, ink_delta: int = 0) -> None:
        ev = self.db.log_action(type_, message, series_id=series_id, level=level,
                                detail=detail, ink_delta=ink_delta)
        ev["kind"] = "action"
        self.bus.publish(ev)
        _LOG_FN.get(level, log.info)("%s%s", message, f" [{series_id}]" if series_id else "")

    def _set_status(self, status: str) -> None:
        self.status = status
        self.bus.publish({"kind": "status", "status": status, "running": self.running})

    def _push_resources(self) -> None:
        self.bus.publish({"kind": "resources", **self.resources})

    def _push_progress(self, progress: list[dict] | None = None) -> None:
        self.bus.publish({"kind": "progress",
                          "series": self.db.progress_view() if progress is None else progress})

    async def sync_status(self) -> dict:
        prev_total = self.resources.get("total_available")
        st = await self.client.get_status()
        self.auth_error = None
        self.resources = {
            "ink": st.get("ink", 0),
            "free_packs": st.get("freePacks", 0),
            "paid_packs": st.get("paidPacks", 0),
            "total_available": st.get("totalAvailable", st.get("freePacks", 0)),
            "level": st.get("level", 0),
            "xp": st.get("xp", 0),
            "xp_to_next": st.get("xpToNext", 0),
            "xp_progress": st.get("xpProgress"),
            "streak": st.get("streak", 0),
            "max_free_packs": st.get("maxFreePacks", 0),
            "next_regen_at": st.get("nextRegenAt"),
        }
        self.db.snapshot_resources(self.resources["ink"], self.resources["free_packs"],
                                   self.resources["paid_packs"], self.resources["level"],
                                   self.resources["xp"])
        log.debug("Status: ink=%d packs=%d/%d level=%d xp=%d streak=%d",
                  self.resources["ink"], self.resources["total_available"],
                  self.resources["max_free_packs"], self.resources["level"],
                  self.resources["xp"], self.resources["streak"])
        new_total = self.resources["total_available"]
        if prev_total is not None and new_total > prev_total:
            self._emit("regen", f"Regeneration: +{new_total - prev_total} pack(s) "
                       f"→ {new_total} available.")
        self._push_resources()
        return st

    async def full_sync(self) -> None:
        self._set_status("syncing")
        self._emit("sync", "Sync in progress…")
        await self.sync_status()
        before = set(self.db.known_series_ids())

        try:
            await self.refresh_series()
        except AuthError:
            raise
        except ApiError as exc:
            self._emit("sync", f"Series list unavailable ({exc}) — using the known series.",
                       level="warn")
        collection = await self.client.get_collection()

        known = set(self.db.known_series_ids())
        for entry in collection:
            sid = entry["series_id"]
            if sid not in known:
                seed = SERIES_SEED.get(sid, {})
                self.db.upsert_series(sid, seed.get("name", _prettify(sid)),
                                      DEFAULT_PRIMARY, DEFAULT_ACCENT, seed.get("size", 0))
            self.db.set_collection_hint(sid, entry.get("owned"), entry.get("total_pulls"))
        self._push_progress()

        for entry in collection:
            await self._load_inventory(entry["series_id"])
            self._push_progress()

        if self.settings.engine.get("fetch_catalog", True):
            for entry in collection:
                if self.db.catalog_size(entry["series_id"]) == 0:
                    await self._load_catalog(entry["series_id"])

        self._push_progress()
        if before:
            self._announce_new_series(
                [sid for sid in self.db.known_series_ids() if sid not in before])
        self._last_full_sync = time.time()
        self._emit("sync", "Sync complete.")
        self._set_status("running" if self.running else "idle")

    async def check_series(self) -> list[str]:
        new_ids = await self.refresh_series()
        for sid in new_ids:
            await self._load_inventory(sid)
        self._announce_new_series(new_ids)
        self._push_progress()
        return new_ids

    async def _load_inventory(self, sid: str) -> None:
        try:
            detail = await self.client.get_series_detail(sid)
        except AuthError:
            raise
        except ApiError as exc:
            self._emit("sync", f"Detail {sid} skipped: {exc}", series_id=sid, level="warn")
            return
        self.db.replace_inventory(sid, [{"card_id": d["card_id"], "rarity": d.get("rarity"),
                                         "quantity": d.get("quantity", 1),
                                         "first_pulled": d.get("first_pulled")}
                                        for d in (detail or [])])

    async def refresh_series(self) -> list[str]:
        self._last_series_check = time.time()
        listing = await self.client.get_series_list()
        sizes = self.db.series_sizes()
        fetch_catalog = self.settings.engine.get("fetch_catalog", True)
        openable: set[str] = set()
        new_ids: list[str] = []
        for s in listing:
            sid = s.get("id")
            if not sid:
                continue
            openable.add(sid)
            name = s.get("name") or SERIES_SEED.get(sid, {}).get("name") or _prettify(sid)
            size = s.get("cardCount")
            size = int(size) if isinstance(size, (int, float)) else 0
            self.db.upsert_series(sid, name, s.get("primaryColor") or DEFAULT_PRIMARY,
                                  s.get("accentColor") or DEFAULT_ACCENT, size)
            old = sizes.get(sid)
            if old is None:
                new_ids.append(sid)
            elif old and size and size != old:
                self._emit("series", f"Series updated: {name} now has {size} cards (was {old}).",
                           series_id=sid, detail={"old_size": old, "size": size})
            cat_n = self.db.catalog_size(sid)
            if fetch_catalog and (cat_n == 0 or (size and cat_n != size)):
                await self._load_catalog(sid)
        if openable:
            self._openable = openable
        return new_ids

    async def _load_catalog(self, sid: str) -> None:
        try:
            cat = await self.client.get_series_catalog(sid)
        except AuthError:
            raise
        except ApiError as exc:
            self._emit("sync", f"Catalog {sid} unavailable: {exc}", series_id=sid, level="warn")
            return
        cards = cat.get("cards", []) if isinstance(cat, dict) else []
        if cards:
            self.db.replace_catalog(sid, cards)

    def _announce_new_series(self, series_ids: list[str]) -> None:
        if not series_ids:
            return
        progress = {p["series_id"]: p for p in self.db.progress_view()}
        for sid in series_ids:
            p = progress.get(sid)
            if not p:
                continue
            by_rarity = " · ".join(f"{r['rarity']} {r['total'] - r['owned']}"
                                   for r in self.db.rarity_breakdown(sid) if r["total"] > r["owned"])
            self._emit("series", f"🆕 New series detected: {p['name']} — {p['missing']} missing "
                       f"card(s) out of {p['total']}" + (f" ({by_rarity})" if by_rarity else ""),
                       series_id=sid, detail={"missing": p["missing"], "total": p["total"]})

    async def _try_buy_packs(self) -> bool:
        cost = int(self.settings.packs.get("full_restock_ink", 400))
        reserve = int(self.settings.engine.get("min_ink_reserve", 0))
        ink = self.resources.get("ink", 0)
        if ink < cost + reserve:
            log.debug("Pack purchase skipped: ink %d < cost %d + reserve %d", ink, cost, reserve)
            return False
        log.info("Buying a restock: ink %d >= cost %d (+ reserve %d)", ink, cost, reserve)
        try:
            res = await self.client.regen_packs("full")
        except ApiError as exc:
            self._emit("buy", f"Pack purchase rejected: {exc}", level="warn")
            return False
        if not (res.get("success") or res.get("freePacks") is not None):
            return False
        self.resources["free_packs"] = res.get("freePacks", self.resources.get("free_packs", 0))
        self.resources["total_available"] = res.get("totalAvailable", self.resources["free_packs"])
        if res.get("newBalance") is not None:
            self.resources["ink"] = res["newBalance"]
        self._emit("buy", f"Restock bought ({cost} ink) → {self.resources['total_available']} packs",
                   ink_delta=-cost, detail={"new_balance": self.resources.get("ink")})
        self._push_resources()
        return True

    def _wake_opener(self) -> None:
        self._wake.set()

    def _on_fatal_auth(self, exc: AuthError) -> None:
        self.auth_error = str(exc)
        self.running = False
        self._emit("error", f"Authentication: {exc} — paste a new session cookie.",
                   level="error")
        self._set_status("error")

    async def _run(self) -> None:
        try:
            await self.full_sync()
            self._set_status("running")
            # gather: if we cancel the supervisor task (stop), the sub-tasks are cancelled too.
            await asyncio.gather(self._open_loop(), self._recycle_loop(),
                                 self._marketplace_loop(), self._status_loop())
        except AuthError as exc:
            self._on_fatal_auth(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Unexpected error in the supervisor")
            self._emit("error", f"Unexpected error: {exc}", level="error")
            self._set_status("error")
        finally:
            self.running = False
            if self.status != "error":
                self._set_status("stopped")

    @staticmethod
    def _fmt_dur(seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        m, r = divmod(s, 60)
        return f"{m}min" if r == 0 else f"{m}min{r:02d}s"

    def _idle_wait_seconds(self) -> float:
        base = float(self.settings.engine.get("idle_poll_seconds", 60.0))
        cap = float(self.settings.engine.get("idle_poll_max", 1800.0))
        regen_ms = self.resources.get("next_regen_at")
        if regen_ms:
            until = regen_ms / 1000.0 - time.time() + 3.0
            if until > base:
                return min(until, cap)
        self._idle_backoff = min(self._idle_backoff * 2, cap) if self._idle_backoff else base
        return self._idle_backoff

    def _pick_target(self, progress: list[dict]) -> dict | None:
        only = self.settings.engine.get("only_series")
        strict = self.settings.engine.get("only_series_mode", "fallback") == "strict"
        # Normal packs only: the mystery pack has its own path (~1x/6h, _try_open_mystery).
        openable = [p for p in progress
                    if (p["series_id"] in self._openable if self._openable is not None
                        else p["series_id"] != "mystery")]
        target = None if only and strict else strategy.select_target_series(openable)
        if target is None and only:
            return next((p for p in progress if p["series_id"] == only), None) or {
                "series_id": only, "name": _prettify(only),
                "missing": 0, "total": 0, "pct": 0.0}
        # Everything complete: keep opening (XP, duplicates for ink) in the best-paying series.
        return target or strategy.select_farm_series(
            openable, self.db.pull_rarities_by_series(), self.settings.recycle.get("values", {}))

    async def _open_loop(self) -> None:
        while self.running:
            try:
                if self.resources.get("total_available", 0) <= 0:
                    await self.sync_status()
                available = self.resources.get("total_available", 0)
                if available > 0:
                    self._idle_backoff = 0.0

                if available <= 0:
                    if self.settings.engine.get("buy_packs_with_ink"):
                        if await self._try_buy_packs():
                            continue
                        if self.settings.engine.get("auto_recycle", True):
                            cost = int(self.settings.packs.get("full_restock_ink", 400))
                            reserve = int(self.settings.engine.get("min_ink_reserve", 0))
                            if await self.recycle_pass(target_ink=cost + reserve) \
                                    and await self._try_buy_packs():
                                continue
                    # Idle wait aligned on the next regen; interruptible via _wake when recycling earns ink.
                    wait = self._idle_wait_seconds()
                    self._set_status("waiting")
                    self._emit("idle", f"No more packs — waiting {self._fmt_dur(wait)} "
                               "(regen/marketplace in parallel; wake up if enough ink).")
                    self._wake.clear()
                    # A wake-up set during the awaits above was just cleared: re-check before sleeping.
                    if self.resources.get("total_available", 0) > 0:
                        self._set_status("running")
                        continue
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=wait)
                        self._idle_backoff = 0.0
                    except asyncio.TimeoutError:
                        pass
                    if self.running:
                        self._set_status("running")
                    continue

                if (self.settings.engine.get("mystery_pack", True)
                        and time.time() >= self._mystery_due_at):
                    await self._try_open_mystery()
                    continue

                target = self._pick_target(self.db.progress_view())
                if target is None:
                    self._emit("done", "No openable series found — stopping.", level="warn")
                    self.running = False
                    break
                if target["series_id"] != self._last_target:
                    self._last_target = target["series_id"]
                    log.info("Target: %s — %d missing/%d (%.1f%%)",
                             target["name"], target["missing"], target["total"], target["pct"])

                try:
                    await self.open_one(target["series_id"])
                except ApiError as exc:
                    self._emit("open", f"Open rejected ({exc}) — resyncing.",
                               series_id=target["series_id"], level="warn")
                    self.resources["total_available"] = 0
                    await self.sync_status()
                    continue

                self._opens_since_resync += 1
                if self._opens_since_resync >= self.settings.engine.get("resync_every", 5):
                    self._opens_since_resync = 0
                    await self.sync_status()
                    fr = float(self.settings.engine.get("full_resync_minutes", 0)) * 60
                    if fr and (time.time() - self._last_full_sync) >= fr:
                        await self.full_sync()
            except AuthError as exc:
                self._on_fatal_auth(exc)
                return
            except asyncio.CancelledError:
                raise
            except ApiError as exc:
                self._emit("open", f"Error (opening): {exc}", level="warn")
                await asyncio.sleep(3.0)
            except Exception as exc:
                log.exception("Error in _open_loop")
                self._emit("error", f"Unexpected error (opening): {exc}", level="error")
                await asyncio.sleep(3.0)

    async def _recycle_loop(self) -> None:
        interval = float(self.settings.engine.get("recycle_interval", 30.0))
        while self.running:
            await asyncio.sleep(interval)
            if not self.running:
                break
            if not (self.settings.engine.get("auto_recycle", True)
                    and self.settings.engine.get("recycle_mode", "surplus") != "on_demand"):
                continue
            # Opening has priority: recycling's one-by-one requests would monopolize the throttle.
            if self.resources.get("total_available", 0) > 0:
                continue
            try:
                log.debug("Recycle tick (every %.0fs)", interval)
                await self.recycle_pass()
            except AuthError as exc:
                self._on_fatal_auth(exc)
                return
            except asyncio.CancelledError:
                raise
            except ApiError as exc:
                self._emit("recycle", f"Error (recycling): {exc}", level="warn")
            except Exception:
                log.exception("Error in _recycle_loop")

    async def _marketplace_loop(self) -> None:
        base = float(self.settings.engine.get("marketplace_interval", 45.0))
        cap = float(self.settings.engine.get("marketplace_interval_max", 900.0))

        def grow() -> float:
            return min((self._mkt_backoff or base) * 2, cap)

        while self.running:
            await asyncio.sleep(self._mkt_backoff or base)
            if not self.running:
                break
            if not self.settings.marketplace.get("enabled"):
                self._mkt_backoff = 0.0
                continue
            # Deferred while packs remain, but guaranteed every marketplace_min_interval (else starved).
            min_interval = float(self.settings.engine.get("marketplace_min_interval", 120.0))
            if (self.resources.get("total_available", 0) > 0
                    and (time.time() - self._last_mkt_pass) < min_interval):
                continue
            try:
                log.debug("Marketplace tick (backoff=%.0fs)", self._mkt_backoff or base)
                self._last_mkt_pass = time.time()
                acted = await self.marketplace_pass()
            except AuthError as exc:
                self._on_fatal_auth(exc)
                return
            except asyncio.CancelledError:
                raise
            except ApiError as exc:
                self._emit("listing", f"Error (marketplace): {exc}", level="warn")
                self._mkt_backoff = grow()
                continue
            except Exception:
                log.exception("Error in _marketplace_loop")
                continue
            self._mkt_backoff = 0.0 if acted else grow()

    async def _status_loop(self) -> None:
        interval = float(self.settings.engine.get("status_sync_seconds", 15.0))
        while self.running:
            await asyncio.sleep(interval)
            if not self.running:
                break
            try:
                await self.sync_status()
                if self.resources.get("total_available", 0) > 0:
                    self._wake_opener()
                check = float(self.settings.engine.get("series_check_minutes", 30.0)) * 60
                if check and time.time() - self._last_series_check >= check:
                    await self.check_series()
            except AuthError as exc:
                self._on_fatal_auth(exc)
                return
            except asyncio.CancelledError:
                raise
            except ApiError:
                pass
            except Exception:
                log.exception("Error in _status_loop")

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._recycle_warned = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._set_status("stopped")
        self._emit("control", "Stop requested.")

    async def sync_now(self) -> None:
        await self.full_sync()
