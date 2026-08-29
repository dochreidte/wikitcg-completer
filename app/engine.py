"""Automation engine (MVP): sync + auto-opening + recycling.

Loop, in "fully automatic" mode:
  1. read the status (ink, free packs, level);
  2. pick the incomplete series missing the most cards;
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

# Event level (_emit) -> logger method; defaults to INFO.
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
        self.status = "idle"          # idle | syncing | running | waiting | stopped | error
        self.resources: dict = {}
        self._task: asyncio.Task | None = None
        self._recycle_warned = False
        self._opens_since_resync = 0
        self.opened_total = 0         # packs opened since startup (multi-account tracking)
        self.recycled_total = 0       # copies recycled since startup (multi-account tracking)
        self._last_target = None
        self.auth_error: str | None = None   # last authentication failure (UI)
        # Cache of my active listings (id + wanted card) — used to quickly cancel a
        # listing as soon as I pull/obtain the card it targeted. Maintained by marketplace_pass.
        self._my_listings: list[dict] = []
        # Copies (pullIds) that returned 500 on recycling (e.g. card committed to a
        # deck) -> epoch until which we do NOT retry them. We re-try after X minutes
        # (recycle_retry_minutes): a card may become recyclable again (removed from a deck...).
        # PERSISTED in the DB (recycle_failures table) -> survives restarts.
        self._recycle_skip: dict[str, float] = self.db.recycle_skips()
        # pullIds already handled this session (recycled SUCCESSFULLY, or 400 "already recycled / last
        # copy"): we NEVER re-submit them, because /api/cards/duplicates lags behind and keeps
        # listing them for a while -> otherwise we'd retry and collect 400 cards_not_found.
        self._recycle_done: set[str] = set()
        # Mystery pack (premium, ~1x/6h): epoch of the next allowed attempt (persisted).
        self._mystery_due_at = float(self.db.get_kv("mystery_due_at", "0") or 0)
        # DAILY recycling quota reached (the server returns 429 on /api/cards/recycle ~200/day):
        # epoch until which we suspend all recycling (persisted). Opening continues regardless.
        self._recycle_quota_until = float(self.db.get_kv("recycle_quota_until", "0") or 0)
        # Growing backoffs (s): wait when there is nothing to do (opening / marketplace).
        self._idle_backoff = 0.0
        self._mkt_backoff = 0.0
        self._last_mkt_pass = 0.0    # epoch of the last marketplace pass actually run
        self._last_full_sync = 0.0   # epoch of the last full_sync (for the periodic re-sync)
        # Wake-up for the opening loop: set by recycling when it has just gained ink
        # while packs are empty -> opening retries the purchase without waiting.
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------ #
    #  Event emission (persisted + pushed to the UI)
    # ------------------------------------------------------------------ #
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

    def _push_progress(self) -> None:
        self.bus.publish({"kind": "progress", "series": self.db.progress_view()})

    # ------------------------------------------------------------------ #
    #  Synchronization
    # ------------------------------------------------------------------ #
    async def sync_status(self) -> dict:
        prev_total = self.resources.get("total_available")
        st = await self.client.get_status()
        self.auth_error = None   # an OK status proves the session is valid
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
        # Tracking: report a regeneration (the pack count went up since the last read).
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
        collection = await self.client.get_collection()

        # 1) IMMEDIATE display: owned/total hints from /api/collection.
        for entry in collection:
            sid = entry["series_id"]
            seed = SERIES_SEED.get(sid, {})
            self.db.upsert_series(sid, seed.get("name", _prettify(sid)),
                                  DEFAULT_PRIMARY, DEFAULT_ACCENT, seed.get("size", 0))
            self.db.set_collection_hint(sid, entry.get("owned"), entry.get("total_pulls"))
        self._push_progress()

        # 2) Per-series detail (exact inventory) — refines progressively (fast reads).
        for entry in collection:
            sid = entry["series_id"]
            try:
                detail = await self.client.get_series_detail(sid)
                items = [{"card_id": d["card_id"], "rarity": d.get("rarity"),
                          "quantity": d.get("quantity", 1),
                          "first_pulled": d.get("first_pulled")} for d in (detail or [])]
                self.db.replace_inventory(sid, items)
                self._push_progress()
            except ApiError as exc:
                self._emit("sync", f"Detail {sid} skipped: {exc}", series_id=sid, level="warn")

        # 3) Catalog (missing cards / marketplace) — loaded afterwards, optional.
        if self.settings.engine.get("fetch_catalog", True):
            for entry in collection:
                sid = entry["series_id"]
                if self.db.catalog_size(sid) == 0:
                    try:
                        cat = await self.client.get_series_catalog(sid)
                        cards = cat.get("cards", []) if isinstance(cat, dict) else []
                        self.db.replace_catalog(sid, cards)
                        if not SERIES_SEED.get(sid, {}).get("size") and cards:
                            self.db.upsert_series(sid, _prettify(sid), DEFAULT_PRIMARY,
                                                  DEFAULT_ACCENT, len(cards))
                    except ApiError as exc:
                        self._emit("sync", f"Catalog {sid} unavailable: {exc}",
                                   series_id=sid, level="warn")

        self._push_progress()
        self._last_full_sync = time.time()
        self._emit("sync", "Sync complete.")
        self._set_status("running" if self.running else "idle")

    # Actions (open_one, _cancel_obsolete_listings, _try_open_mystery, recycle_pass)
    #   -> engine_actions.ActionsMixin
    # Marketplace (marketplace_pass, _fulfill_useful)
    #   -> engine_market.MarketMixin

    # ------------------------------------------------------------------ #
    #  Buying packs with ink (restock)
    # ------------------------------------------------------------------ #
    async def _try_buy_packs(self) -> bool:
        """Buy a full restock if ink allows (keeping a reserve). Ink is used ONLY for this.
        Returns True if a purchase happened."""
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

    # ------------------------------------------------------------------ #
    #  Main loop — 3 PARALLEL tasks (opening / recycling / marketplace)
    #
    #  All three run concurrently and share the client: the global throttle already
    #  SERIALIZES every HTTP request (anti-429), so parallelism does not increase network
    #  throughput — it DECOUPLES the cadences: the marketplace and recycling stay responsive
    #  even while opening waits for regeneration, and a pulled card immediately cancels the
    #  corresponding listing. Cooperative stop via self.running; an AuthError stops everything
    #  (retrying is pointless).
    # ------------------------------------------------------------------ #
    def _wake_opener(self) -> None:
        """Wake the opening loop if it is sleeping (e.g. after an ink gain)."""
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
        except Exception as exc:  # safety net: never let the supervisor crash the app
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
        """How long to wait when there is nothing left to do on the opening/buying side.
        We aim for the NEXT known free pack (nextRegenAt); otherwise a capped growing backoff."""
        base = float(self.settings.engine.get("idle_poll_seconds", 60.0))
        cap = float(self.settings.engine.get("idle_poll_max", 1800.0))
        regen_ms = self.resources.get("next_regen_at")
        if regen_ms:
            until = regen_ms / 1000.0 - time.time() + 3.0   # just after the regen
            if until > base:
                return min(until, cap)
        # no usable regen info -> ramp up progressively
        self._idle_backoff = min(self._idle_backoff * 2, cap) if self._idle_backoff else base
        return self._idle_backoff

    async def _open_loop(self) -> None:
        """Open the available packs per the strategy; when there are none left, buy with ink
        (funding via recycling if needed) or wait for regeneration."""
        while self.running:
            try:
                if self.resources.get("total_available", 0) <= 0:
                    await self.sync_status()
                available = self.resources.get("total_available", 0)
                if available > 0:
                    self._idle_backoff = 0.0   # progress possible -> back to normal cadence

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
                    if self.settings.engine.get("on_empty") == "stop":
                        self._emit("idle", "No more packs — stopping (on_empty=stop).")
                        self.running = False
                        break
                    # Nothing to do here (no ink and no affordable recycling): wait.
                    # We align on the NEXT known free pack (nextRegenAt); otherwise a capped
                    # growing backoff (avoids reloading every minute).
                    # BUT the wait is INTERRUPTIBLE: if recycling earns enough ink, it sets
                    # `_wake` and we wake up immediately to buy (real coordination).
                    wait = self._idle_wait_seconds()
                    self._set_status("waiting")
                    self._emit("idle", f"No more packs — waiting {self._fmt_dur(wait)} "
                               "(regen/marketplace in parallel; wake up if enough ink).")
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=wait)
                        self._idle_backoff = 0.0   # woken by an ink gain -> retry quickly
                    except asyncio.TimeoutError:
                        pass
                    if self.running:
                        self._set_status("running")
                    continue

                # Mystery pack (premium, ~1x/6h): priority as soon as it's due and a pack is
                # available — we dedicate one pack to it before the normal series.
                if (self.settings.engine.get("mystery_pack", True)
                        and time.time() >= self._mystery_due_at):
                    await self._try_open_mystery()
                    continue   # the pack count changed -> loop again

                progress = self.db.progress_view()
                only = self.settings.engine.get("only_series")
                if only:
                    # Single-series mode (multi-account orchestrator): ALWAYS open this series,
                    # even when complete (duplicates become trade material for the LR).
                    target = next((p for p in progress if p["series_id"] == only), None) or {
                        "series_id": only, "name": _prettify(only),
                        "missing": 0, "total": 0, "pct": 0.0}
                else:
                    target = strategy.select_target_series(progress)
                    if target is None:
                        self._emit("done", "All known series are complete 🎉")
                        self.running = False
                        break
                if target["series_id"] != self._last_target:
                    self._last_target = target["series_id"]
                    log.info("Target: %s — %d missing/%d (%.1f%%) [most profitable series]",
                             target["name"], target["missing"], target["total"], target["pct"])

                # Top priority: open the available packs. The marketplace is only a
                # COMPLEMENTARY TOOL (parallel task) — it never interrupts opening.
                if self.settings.engine.get("auto_open", True):
                    try:
                        await self.open_one(target["series_id"])
                    except ApiError as exc:
                        self._emit("open", f"Open rejected ({exc}) — resyncing.",
                                   series_id=target["series_id"], level="warn")
                        self.resources["total_available"] = 0
                        await self.sync_status()
                        continue

                # periodic status resync (real ink / packs)
                self._opens_since_resync += 1
                if self._opens_since_resync >= self.settings.engine.get("resync_every", 5):
                    self._opens_since_resync = 0
                    await self.sync_status()
                    # periodic FULL re-sync (opt-in): realign inventory/series if you also
                    # play in the browser. 0 = disabled.
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
        """Periodic recycling of the surplus ("surplus" mode). In "on_demand", does nothing:
        recycling then only happens to fund a restock (in _open_loop)."""
        interval = float(self.settings.engine.get("recycle_interval", 30.0))
        while self.running:
            await asyncio.sleep(interval)
            if not self.running:
                break
            if not (self.settings.engine.get("auto_recycle", True)
                    and self.settings.engine.get("recycle_mode", "surplus") != "on_demand"):
                continue
            # OPENING HAS PRIORITY: while packs remain to open, defer recycling (otherwise its
            # one-by-one requests monopolize the throttle and delay opening). Recycling thus
            # runs mostly when packs are exhausted (and funds purchases).
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
        """Periodic upkeep of the listings (create/fulfill/cancel obsolete ones).
        Does nothing while [marketplace] enabled is false (adjustable live).

        Capped growing backoff when a pass does NOTHING (nothing to create or fulfill):
        we don't re-probe other players' market every N s for nothing. Fast cadence is
        restored as soon as an action happens."""
        base = float(self.settings.engine.get("marketplace_interval", 45.0))
        cap = float(self.settings.engine.get("marketplace_interval_max", 900.0))

        def grow() -> float:   # space out the next pass (capped growing backoff)
            return min((self._mkt_backoff or base) * 2, cap)

        while self.running:
            await asyncio.sleep(self._mkt_backoff or base)
            if not self.running:
                break
            if not self.settings.marketplace.get("enabled"):
                self._mkt_backoff = 0.0
                continue
            # Opening has priority BUT a GUARANTEED marketplace cadence: while packs remain to
            # open, defer the marketplace — unless the last pass was more than
            # `marketplace_min_interval` ago (otherwise, with near-infinite ink, opening never
            # stops and the marketplace is never served). So we guarantee it >= 1 pass / N s.
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
            # action -> fast cadence; nothing -> space out (up to the cap)
            self._mkt_backoff = 0.0 if acted else grow()

    async def _status_loop(self) -> None:
        """LIGHT and frequent status re-sync (real ink / packs / regen) — to always know the
        real numbers and detect a regeneration without depending on the other loops. Cheap GET;
        cadence `status_sync_seconds`."""
        interval = float(self.settings.engine.get("status_sync_seconds", 15.0))
        while self.running:
            await asyncio.sleep(interval)
            if not self.running:
                break
            try:
                await self.sync_status()
                # Regen detected during the wait -> wake the opener to open right away.
                if self.resources.get("total_available", 0) > 0:
                    self._wake_opener()
            except AuthError as exc:
                self._on_fatal_auth(exc)
                return
            except asyncio.CancelledError:
                raise
            except ApiError:
                pass
            except Exception:
                log.exception("Error in _status_loop")

    # ------------------------------------------------------------------ #
    #  Control
    # ------------------------------------------------------------------ #
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
