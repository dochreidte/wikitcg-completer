"""Engine actions: opening packs, mystery pack, recycling."""
from __future__ import annotations

import logging
import time

from .api_client import ApiError
from .series_seed import DEFAULT_PRIMARY, DEFAULT_ACCENT
from . import strategy

log = logging.getLogger("wikitcg.engine")


class ActionsMixin:
    async def open_one(self, series_id: str, *, label: str = "Booster", kind: str = "open") -> None:
        """Open a pack and record the pulled cards."""
        res = await self.client.open_pack(series_id)
        # A 200 with an {error} field (e.g. mystery cooldown) is a failure: don't decrement packs.
        if isinstance(res, dict) and res.get("error"):
            raise ApiError(f"Pack open rejected: {res['error']}", body=str(res.get("error")))
        cards = res.get("cards", [])
        s = res.get("series") or {}
        if s.get("name"):
            self.db.upsert_series(series_id, s["name"],
                                  s.get("primaryColor", DEFAULT_PRIMARY),
                                  s.get("accentColor", DEFAULT_ACCENT), 0)
        new_cards, dup_cards = [], []
        for c in cards:
            cid, rarity = c.get("id"), c.get("rarity")
            was_new = self.db.bump_inventory(series_id, cid, rarity)
            self.db.log_pull(series_id, cid, rarity, was_new)
            (new_cards if was_new else dup_cards).append({"id": cid, "rarity": rarity,
                                                          "title": c.get("article", {}).get("title")})
        if res.get("totalAvailable") is not None:
            self.resources["total_available"] = res["totalAvailable"]
            self.resources["free_packs"] = res.get("freePacks", self.resources.get("free_packs", 0))
        else:
            self.resources["free_packs"] = max(self.resources.get("free_packs", 1) - 1, 0)
            self.resources["total_available"] = max(self.resources.get("total_available", 1) - 1, 0)
        self.opened_total += 1
        self._emit(kind, f"{label} opened: {len(new_cards)} new, {len(dup_cards)} duplicate(s)",
                   series_id=series_id, detail={"new": new_cards, "dup": dup_cards,
                                                "xp": res.get("xpEarned"), "streak": res.get("streak")})
        rares = [c for c in new_cards if c["rarity"] in ("SSR", "UR", "LR")]
        for c in rares:
            self._emit("rare", f"✨ Rare card: {c['rarity']} {c.get('title') or c['id']}",
                       series_id=series_id, detail=c)
        lvl = res.get("levelUp")
        if lvl:
            new_lvl = lvl.get("level") if isinstance(lvl, dict) else lvl
            self._emit("level", f"⬆️ Level {new_lvl} reached!", detail={"levelUp": lvl})
        progress = self.db.progress_view()
        if new_cards:
            p = next((p for p in progress if p["series_id"] == series_id), None)
            if p and p["total"] and not p["missing"]:
                self._emit("complete", f"🏁 Series complete: {p['name']} ({p['owned']}/{p['total']})",
                           series_id=series_id)
        self._push_resources()
        self._push_progress(progress)
        if self._my_listings and new_cards:
            await self._cancel_obsolete_listings({c["id"] for c in new_cards})

    async def _cancel_obsolete_listings(self, acquired_ids: set[str]) -> None:
        remaining: list[dict] = []
        for l in self._my_listings:
            if l.get("wanted_card_id") in acquired_ids:
                try:
                    await self.client.marketplace_cancel(l["id"])
                    self._emit("listing", f"Listing cancelled: {l['wanted_card_id']} obtained in a pack",
                               detail=l)
                except ApiError as exc:
                    self._emit("listing", f"Cancellation rejected: {exc}", level="warn", detail=l)
                    remaining.append(l)
            else:
                remaining.append(l)
        self._my_listings = remaining

    async def _try_open_mystery(self) -> bool:
        interval = float(self.settings.engine.get("mystery_interval_hours", 6.0)) * 3600
        try:
            await self.open_one("mystery", label="Mystery pack", kind="mystery")
        except ApiError as exc:
            self._mystery_due_at = time.time() + 1800
            self.db.set_kv("mystery_due_at", str(self._mystery_due_at))
            self._emit("mystery", f"Mystery pack unavailable ({exc}) — will retry later.",
                       level="warn")
            return False
        self._mystery_due_at = time.time() + interval
        self.db.set_kv("mystery_due_at", str(self._mystery_due_at))
        return True

    async def recycle_pass(self, target_ink: int | None = None) -> int:
        quota_until = self._recycle_quota_until
        if quota_until > time.time():
            log.debug("Recycling paused (daily quota) — resuming in ~%d min.",
                      int((quota_until - time.time()) / 60) + 1)
            return 0
        dups = await self.client.get_duplicates(self.settings.recycle)
        if not dups:
            return 0
        dups = [d for d in dups if d.get("pull_ids")]
        if not dups:
            if not self._recycle_warned:
                self._recycle_warned = True
                self._emit("recycle", "Unrecognized /api/cards/duplicates response: "
                           "recycling suspended (paste a sample to finalize parsing).",
                           level="warn")
            return 0

        keep = self.settings.engine.get("keep_spares", {})
        values = self.settings.recycle.get("values", {})
        now = time.time()
        mkt_on = bool(self.settings.marketplace.get("enabled"))
        skip_rarities = set(self.settings.engine.get("recycle_skip_rarities", []))
        # "fixed": keep_spares per card; "missing": per rarity, keep as many spares as missing cards.
        reserve_mode = self.settings.engine.get("recycle_reserve_mode", "fixed")
        missing_by_rar = self.db.missing_by_rarity() if reserve_mode == "missing" else {}

        # Keep every pullId outside cooldown so another copy can be tried if one is locked (500).
        groups: list[dict] = []
        extras_by_rar: dict[str, int] = {}
        skipped_cooldown = 0
        for d in dups:
            rarity = d.get("rarity") or ""
            if rarity in skip_rarities:
                continue
            copies = d.get("copies", len(d["pull_ids"]))
            if reserve_mode == "missing":
                surplus = copies - 1
            else:
                reserve = keep.get(rarity, 0)
                if mkt_on and rarity in strategy.TRADEABLE_RARITIES:
                    reserve = max(reserve, 1)
                surplus = copies - 1 - reserve
            if surplus <= 0:
                continue
            pulls = []
            for pid in d["pull_ids"]:
                if pid in self._recycle_done:
                    continue
                if self._recycle_skip.get(pid, 0) > now:
                    skipped_cooldown += 1
                else:
                    pulls.append(pid)
            if pulls:
                extras_by_rar[rarity] = extras_by_rar.get(rarity, 0) + surplus
                groups.append({"card_id": d.get("card_id"), "rarity": rarity,
                               "series_id": d.get("series_id"), "surplus": surplus, "pulls": pulls})

        rarity_budget = {}
        if reserve_mode == "missing":
            for r, extras in extras_by_rar.items():
                rarity_budget[r] = max(extras - missing_by_rar.get(r, 0), 0)

        if not groups:
            if skipped_cooldown:
                log.debug("Recycling: %d copy(ies) in 500 cooldown, retry later.",
                          skipped_cooldown)
            else:
                log.debug("Recycling: no surplus beyond the reserve (%d type(s))", len(dups))
            return 0
        # "common_first" preserves trade material; "rare_first" maximizes ink within the daily quota.
        rare_first = self.settings.engine.get("recycle_priority", "common_first") == "rare_first"
        groups.sort(key=lambda g: strategy.RARITY_RANK.get(g["rarity"], 99), reverse=rare_first)
        cap = int(self.settings.recycle.get("max_per_call", 20))
        if target_ink is not None:
            log.info("On-demand recycling: targeting %d ink (currently %d).",
                     target_ink, self.resources.get("ink", 0))

        retry_after = float(self.settings.engine.get("recycle_retry_minutes", 30.0)) * 60
        recycled: list[str] = []
        failed, total_ink, new_balance, stop = 0, 0, None, False
        rarity_used: dict[str, int] = {}
        for g in groups:
            if stop:
                break
            r = g["rarity"]
            card_cap = g["surplus"]
            if reserve_mode == "missing":
                card_cap = min(card_cap, rarity_budget.get(r, 0) - rarity_used.get(r, 0))
            if card_cap <= 0:
                continue
            got = 0
            for pid in g["pulls"]:
                if got >= card_cap:
                    break
                if len(recycled) >= cap:
                    stop = True
                    break
                if target_ink is not None and new_balance is not None and new_balance >= target_ink:
                    stop = True
                    break
                try:
                    res = await self.client.recycle([pid], retry_5xx=False, retry_429=False)
                except ApiError as exc:
                    if getattr(exc, "status", None) == 429:
                        # 429 = daily recycle quota reached: pause recycling instead of hammering card by card.
                        cd = float(self.settings.engine.get("recycle_quota_cooldown_minutes", 60.0)) * 60
                        self._recycle_quota_until = time.time() + cd
                        self.db.set_kv("recycle_quota_until", str(self._recycle_quota_until))
                        self._emit("recycle", "Daily recycle quota reached — recycling paused "
                                   f"for {int(cd / 60)} min (opening continues).", level="warn")
                        stop = True
                        break
                    body = getattr(exc, "body", "") or ""
                    if getattr(exc, "status", None) == 400 and (
                            "cards_not_found" in body or "cannot_recycle_last_copy" in body):
                        # Benign 400 (stale /duplicates: already recycled or last copy): mark done, never retry.
                        self._recycle_done.add(pid)
                        reason = "cards_not_found" if "cards_not_found" in body else "cannot_recycle_last_copy"
                        log.debug("Recycling: copy %s (%s) skipped (400 %s — already recycled / "
                                  "last copy).", g["card_id"], g["rarity"], reason)
                        continue
                    # 500 / unexpected 4xx: copy locked server-side -> growing persisted cooldown, try next copy.
                    now_f = time.time()
                    retry_at = self.db.mark_recycle_failure(pid, g["card_id"], g["rarity"],
                                                            retry_after, now_f)
                    self._recycle_skip[pid] = retry_at
                    failed += 1
                    log.debug("Recycling: copy %s (%s) not recyclable, retry at +%d min — %s",
                              g["card_id"], g["rarity"], int((retry_at - now_f) / 60), exc)
                    continue
                total_ink += res.get("inkEarned", 0)
                if res.get("newBalance") is not None:
                    new_balance = res["newBalance"]
                self.db.log_recycle(pid, g["card_id"], g["rarity"], int(values.get(g["rarity"], 0)))
                if g["series_id"] and g["card_id"]:
                    self.db.decrement_inventory(g["series_id"], g["card_id"], by=1)
                self._recycle_skip.pop(pid, None)
                self.db.clear_recycle_failure(pid)
                self._recycle_done.add(pid)
                recycled.append(pid)
                got += 1
                rarity_used[r] = rarity_used.get(r, 0) + 1

        if not recycled:
            if failed:
                self._emit("recycle", f"{failed} card(s) with a 500 error — retry in "
                           f"{int(retry_after/60)} min.", level="warn")
            return 0
        self.recycled_total += len(recycled)
        suffix = f" ({failed} failed, retry +{int(retry_after/60)} min)" if failed else ""
        if new_balance is not None:
            self.resources["ink"] = new_balance
        self._emit("recycle", f"{len(recycled)} duplicate(s) recycled (+{total_ink} ink){suffix}",
                   ink_delta=total_ink, detail={"new_balance": new_balance, "failed": failed})
        self._push_resources()
        self._push_progress()
        if total_ink and self.resources.get("total_available", 0) <= 0:
            self._wake_opener()
        return len(recycled)
