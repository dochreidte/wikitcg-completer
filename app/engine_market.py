"""Marketplace part of the engine (mixin of `Engine`).

Verified contracts (site frontend + HAR captures + live create->cancel test):
  create  : POST /api/marketplace/create {offeredCardTypeId, offeredSeriesId,
            wantedCardId, wantedSeriesId}  -> 201 {success}
  cancel  : POST /api/marketplace/{id}/cancel -> 200 {success}
  fulfill : POST /api/marketplace/{id}/fulfill (gives a card to another player — irreversible).
Disabled by default: commits real cards and affects other real players.
`dry_run` mode: logs what WOULD be done, without any writes.
"""
from __future__ import annotations

import logging
import time

from .api_client import ApiError
from . import marketplace

log = logging.getLogger("wikitcg.engine")


class MarketMixin:
    """Marketplace methods, mixed into `Engine` (access via self.client/db/settings/_emit...)."""

    async def marketplace_pass(self) -> int:
        """Maintains the listing pool. Returns the number of ACTIONS performed
        (cancels + fulfills + creates) — used by the marketplace loop's backoff."""
        cfg = self.settings.marketplace
        if not cfg.get("enabled"):
            return 0
        dry = bool(cfg.get("dry_run"))      # validation mode: log without writing
        cancels = fulfills = creates = 0
        mine = await self.client.marketplace_mine()
        listings = mine.get("listings") or []

        # 0a) FULFILLED listings: I received the wanted card. Credit it to the inventory (once,
        #     persistent tracking) so we do NOT recreate the offer afterwards (bug "recreates an
        #     already-fulfilled listing"). Also decrement the offered (given) copy.
        seen = set(self.db.get_json("fulfilled_seen", []) or [])
        changed = False
        for l in listings:
            if (l.get("status") == "fulfilled" or l.get("fulfilled_by")) and l["id"] not in seen:
                wc, ws = l.get("wanted_card_id"), l.get("wanted_series_id")
                if wc and ws and not self.db.owns_card(wc):
                    self.db.bump_inventory(ws, wc, l.get("wanted_rarity"))
                if l.get("offered_card_type") and l.get("offered_series"):
                    self.db.decrement_inventory(l["offered_series"], l["offered_card_type"], by=1)
                self._emit("fulfill", f"Listing fulfilled by another player: I received {wc}",
                           detail=l)
                seen.add(l["id"]); changed = True
        if changed:
            self.db.set_json("fulfilled_seen", list(seen)[-1000:])  # cap the size

        active = [l for l in listings if l.get("status") == "active"]

        # 0b) Sweep the active listings: cancel those that became POINTLESS (wanted card already
        #     obtained) OR TOO OLD (> max_listing_age_hours) — they'll be recreated fresh.
        now_ms = time.time() * 1000
        max_age_ms = float(cfg.get("max_listing_age_hours", 24)) * 3600 * 1000
        still_active = []
        for l in active:
            wc = l.get("wanted_card_id")
            owned = wc and self.db.owns_card(wc)
            too_old = max_age_ms > 0 and (now_ms - (l.get("created_at") or now_ms)) > max_age_ms
            if owned or too_old:
                reason = "card already obtained" if owned else "listing > 1 day (renewal)"
                if dry:
                    self._emit("listing", f"[simulated] would cancel: {reason}", detail=l)
                    continue
                try:
                    await self.client.marketplace_cancel(l["id"])
                    self._emit("listing", f"Listing cancelled: {reason}", detail=l)
                    cancels += 1
                    continue
                except ApiError as exc:
                    self._emit("listing", f"Cancellation rejected: {exc}", level="warn", detail=l)
            still_active.append(l)
        active = still_active

        # Cache for the immediate cancellation on the opening side (open_one).
        self._my_listings = [{"id": l["id"], "wanted_card_id": l.get("wanted_card_id")}
                             for l in active]
        self.resources["listings_active"] = len(active)
        self._push_resources()

        # 1) Fulfill useful listings (instant acquisition), if enabled.
        if cfg.get("fulfill_others"):
            fulfills = await self._fulfill_useful(cfg)

        # 2) Refill up to max_listings active listings.
        max_listings = int(cfg.get("max_listings", 5))
        n_needed = max_listings - len(active)
        if n_needed > 0:
            committed: dict[str, int] = {}
            for l in active:
                t = l.get("offered_card_type")
                committed[t] = committed.get(t, 0) + 1
            existing_wanted = {l.get("wanted_card_id") for l in active}
            progress = self.db.progress_view()
            # Focus: target ONLY series close to completion (<= N missing cards).
            near = int(cfg.get("near_completion_max_missing", 0))
            all_missing = self.db.missing_cards_grouped()   # one query instead of one per series
            missing_by_series = {p["series_id"]: all_missing.get(p["series_id"], [])
                                 for p in progress
                                 if p["missing"] > 0 and (near <= 0 or p["missing"] <= near)}
            dups = self.db.all_duplicates()
            plans = marketplace.plan_listings(missing_by_series, dups, committed, existing_wanted,
                                              n_needed, progress, cfg.get("prefer_near_completion", True))
            # We offer a card TYPE (the server commits one copy). Verified contract.
            for pl in plans:
                if dry:
                    self._emit("listing",
                               f"[simulated] would create: offering {pl['offered_type']} for {pl['wanted_card']}",
                               series_id=pl["wanted_series"], detail=pl)
                    continue
                try:
                    await self.client.marketplace_create(pl["offered_type"], pl["offered_series"],
                                                         pl["wanted_card"], pl["wanted_series"])
                    self._emit("listing",
                               f"Listing created: offering {pl['offered_type']} for {pl['wanted_card']}",
                               series_id=pl["wanted_series"], detail=pl)
                    creates += 1
                except ApiError as exc:
                    self._emit("listing", f"Listing creation rejected: {exc}", level="warn", detail=pl)
                    break

        actions = cancels + fulfills + creates
        # Pass summary (tracking) — only if something happened, otherwise silent.
        if actions:
            self._emit("market", f"Marketplace: {len(active)} active · "
                       f"{creates} created, {fulfills} fulfilled, {cancels} cancelled",
                       detail={"active": len(active), "created": creates,
                               "fulfilled": fulfills, "cancelled": cancels})
        return actions

    async def _fulfill_useful(self, cfg: dict) -> int:
        """Fulfill useful listings from other players. Returns the number of trades done.

        Invariant (user rule): fulfill ONLY if (a) I own the wanted card as a duplicate AND
        (b) I do not already own the offered card (cf. marketplace.plan_fulfillments)."""
        browse = await self.client.marketplace_browse()
        listings = browse.get("listings") or []
        my_missing = self.db.missing_card_ids()   # all missing cards in one query
        spare_by_type = {d["card_id"]: d["quantity"] - 1 for d in self.db.all_duplicates()}
        deals = marketplace.plan_fulfillments(listings, my_missing, spare_by_type,
                                              int(cfg.get("fulfill_per_pass", 3)))
        dry = bool(cfg.get("dry_run"))
        done = 0
        for deal in deals:
            if dry:
                self._emit("fulfill",
                           f"[simulated] would fulfill: gain {deal['gain_card']} for {deal['give_card']}",
                           detail=deal)
                continue
            try:
                await self.client.marketplace_fulfill(deal["listing_id"])
                self._emit("fulfill",
                           f"Listing fulfilled: I gain {deal['gain_card']} for {deal['give_card']}",
                           detail=deal)
                done += 1
            except ApiError as exc:
                self._emit("fulfill", f"Listing fulfillment failed: {exc}", level="warn", detail=deal)
        return done
