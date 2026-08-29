"""Engine actions (mixin of `Engine`): opening packs, mystery pack, recycling.

Mixed into `Engine`: accesses self.client / db / settings / resources / _emit / _push_* /
_recycle_skip / _my_listings / _recycle_warned / _mystery_due_at.
"""
from __future__ import annotations

import logging
import time

from .api_client import ApiError
from .series_seed import SERIES_SEED, DEFAULT_PRIMARY, DEFAULT_ACCENT
from . import strategy

log = logging.getLogger("wikitcg.engine")


class ActionsMixin:
    async def open_one(self, series_id: str, *, label: str = "Booster", kind: str = "open") -> None:
        res = await self.client.open_pack(series_id)
        # Opening can return 200 with an {error} field (e.g. mystery-pack cooldown, no more
        # packs...). We treat it as a failure so we do NOT wrongly decrement.
        if isinstance(res, dict) and res.get("error"):
            raise ApiError(f"Pack open rejected: {res['error']}", body=str(res.get("error")))
        cards = res.get("cards", [])
        # series meta (name + colors) if available
        s = res.get("series") or {}
        if s.get("name"):
            size = SERIES_SEED.get(series_id, {}).get("size") or self.db.catalog_size(series_id)
            self.db.upsert_series(series_id, s["name"],
                                  s.get("primaryColor", DEFAULT_PRIMARY),
                                  s.get("accentColor", DEFAULT_ACCENT), size)
        new_cards, dup_cards = [], []
        for c in cards:
            cid, rarity = c.get("id"), c.get("rarity")
            was_new = self.db.bump_inventory(series_id, cid, rarity)
            self.db.log_pull(series_id, cid, rarity, was_new)
            (new_cards if was_new else dup_cards).append({"id": cid, "rarity": rarity,
                                                          "title": c.get("article", {}).get("title")})
        # Pack count: prefer the number returned by the server (reliable); otherwise
        # decrement locally (optimistic).
        if res.get("totalAvailable") is not None:
            self.resources["total_available"] = res["totalAvailable"]
            self.resources["free_packs"] = res.get("freePacks", self.resources.get("free_packs", 0))
        else:
            self.resources["free_packs"] = max(self.resources.get("free_packs", 1) - 1, 0)
            self.resources["total_available"] = max(self.resources.get("total_available", 1) - 1, 0)
        self.opened_total += 1   # multi-account tracking: did we open anything this session?
        self._emit(kind, f"{label} opened: {len(new_cards)} new, {len(dup_cards)} duplicate(s)",
                   series_id=series_id, detail={"new": new_cards, "dup": dup_cards,
                                                "xp": res.get("xpEarned"), "streak": res.get("streak")})
        # Tracking: highlight RARE cards pulled (SSR and above) and level-ups.
        rares = [c for c in new_cards if c["rarity"] in ("SSR", "UR", "LR")]
        for c in rares:
            self._emit("rare", f"✨ Rare card: {c['rarity']} {c.get('title') or c['id']}",
                       series_id=series_id, detail=c)
        lvl = res.get("levelUp")
        if lvl:
            new_lvl = lvl.get("level") if isinstance(lvl, dict) else lvl
            self._emit("level", f"⬆️ Level {new_lvl} reached!", detail={"levelUp": lvl})
        self._push_resources()
        self._push_progress()
        # Promptly cancel any listing made pointless: I just pulled the card it wanted.
        if self._my_listings and new_cards:
            await self._cancel_obsolete_listings({c["id"] for c in new_cards})

    async def _cancel_obsolete_listings(self, acquired_ids: set[str]) -> None:
        """Cancel active listings whose WANTED card was just obtained."""
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
        """Open the 'mystery' pack (premium, ~1x/6h) via /api/packs/open seriesId=mystery —
        like a normal pack (consumes one available pack, 0 ink). On cooldown/refusal, retry
        later without spamming. The due time is persisted (survives restart)."""
        interval = float(self.settings.engine.get("mystery_interval_hours", 6.0)) * 3600
        try:
            await self.open_one("mystery", label="Mystery pack", kind="mystery")
        except ApiError as exc:
            self._mystery_due_at = time.time() + 1800   # cooldown/refusal: retry in ~30 min
            self.db.set_kv("mystery_due_at", str(self._mystery_due_at))
            self._emit("mystery", f"Mystery pack unavailable ({exc}) — will retry later.",
                       level="warn")
            return False
        self._mystery_due_at = time.time() + interval
        self.db.set_kv("mystery_due_at", str(self._mystery_due_at))
        return True

    async def recycle_pass(self, target_ink: int | None = None) -> int:
        """GLOBAL recycling: fetches duplicates via /api/cards/duplicates, applies the
        keep_spares policy, recycles the surplus. Returns the number recycled.

        Each duplicate = {card_id, series_id, rarity, copies, pull_ids:[...]}; we recycle the
        first `copies - 1 - reserve` copies (pull_ids). If the response can't be interpreted
        (no copy identified) -> warning, 0 recycled.

        If `target_ink` is given (on-demand recycling), we recycle ONLY the minimum needed to
        reach that ink amount, sacrificing the LEAST valuable copies first — to preserve
        valuable cards (trade material).
        """
        # DAILY recycle quota recently hit (429) -> suspend: no point calling /duplicates or
        # /recycle, everything would return 429. Opening keeps going.
        quota_until = self._recycle_quota_until
        if quota_until > time.time():
            log.debug("Recycling paused (daily quota) — resuming in ~%d min.",
                      int((quota_until - time.time()) / 60) + 1)
            return 0
        dups = await self.client.get_duplicates(self.settings.recycle)
        if not dups:
            return 0
        if all(not d.get("pull_ids") for d in dups):
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
        # Reserve: "fixed" = keep_spares per card (+marketplace floor); "missing" = keep, PER
        # RARITY, as many duplicates as there are missing cards of that rarity (trade material
        # to acquire the missing ones). E.g. 15 duplicate LR, 3 missing -> recycle 12.
        reserve_mode = self.settings.engine.get("recycle_reserve_mode", "fixed")
        missing_by_rar = self.db.missing_by_rarity() if reserve_mode == "missing" else {}

        # For each TYPE: `surplus` = number of recyclable copies of this card (we always keep
        # 1 collection copy). We keep ALL pullIds (outside cooldown) so we can try ANOTHER copy
        # if one is locked (500).
        groups: list[dict] = []
        extras_by_rar: dict[str, int] = {}
        skipped_cooldown = 0
        for d in dups:
            rarity = d.get("rarity") or ""
            if rarity in skip_rarities:   # excluded rarity (e.g. LR) -> never recycle
                continue
            copies = d.get("copies", len(d["pull_ids"]))
            if reserve_mode == "missing":
                surplus = copies - 1                       # all extras; reserve applied per rarity
            else:
                reserve = keep.get(rarity, 0)
                if mkt_on and rarity in strategy.TRADEABLE_RARITIES:
                    reserve = max(reserve, 1)
                surplus = copies - 1 - reserve
            if surplus <= 0:
                continue
            pulls = []
            for pid in d["pull_ids"]:
                if pid in self._recycle_done:              # already recycled/exhausted this session
                    continue                                # (stale /duplicates list) -> ignore
                if self._recycle_skip.get(pid, 0) > now:   # in cooldown after a 500
                    skipped_cooldown += 1
                else:
                    pulls.append(pid)
            if pulls:
                extras_by_rar[rarity] = extras_by_rar.get(rarity, 0) + surplus
                groups.append({"card_id": d.get("card_id"), "rarity": rarity,
                               "series_id": d.get("series_id"), "surplus": surplus, "pulls": pulls})

        # "missing" mode: recyclable budget PER RARITY = total extras - number missing.
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
        # Recycle order by rarity:
        #  * "common_first" (default): sacrifice the least rare first -> preserves valuable
        #    cards (trade material);
        #  * "rare_first" (pure farm): recycle the rarest first -> MAXIMUM ink extracted within
        #    the daily quota (~200 recycles/day) since LR/UR are worth the most.
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
            if reserve_mode == "missing":   # capped by the rarity's remaining budget
                card_cap = min(card_cap, rarity_budget.get(r, 0) - rarity_used.get(r, 0))
            if card_cap <= 0:
                continue
            got = 0   # number recycled for THIS type (<= card_cap)
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
                        # 429 = DAILY recycle quota reached (~200/day): every card would return
                        # 429. We put recycling on a long pause (instead of hammering card by
                        # card); opening keeps going in parallel.
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
                        # BENIGN 400: copy already recycled (stale /duplicates list) or only 1
                        # copy left (stale count). This is NOT an error: mark the pullId as
                        # "done" (never retried again) and move on, without alarm.
                        self._recycle_done.add(pid)
                        reason = "cards_not_found" if "cards_not_found" in body else "cannot_recycle_last_copy"
                        log.debug("Recycling: copy %s (%s) skipped (400 %s — already recycled / "
                                  "last copy).", g["card_id"], g["rarity"], reason)
                        continue
                    # 500 (or other unexpected 4xx): non-recyclable copy (locked server-side)
                    # -> growing persisted cooldown; try the NEXT copy.
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
                self._recycle_done.add(pid)   # never resubmit it (stale /duplicates list)
                recycled.append(pid)
                got += 1
                rarity_used[r] = rarity_used.get(r, 0) + 1

        if not recycled:
            if failed:
                self._emit("recycle", f"{failed} card(s) with a 500 error — retry in "
                           f"{int(retry_after/60)} min.", level="warn")
            return 0
        self.recycled_total += len(recycled)   # multi-account tracking
        suffix = f" ({failed} failed, retry +{int(retry_after/60)} min)" if failed else ""
        if new_balance is not None:
            self.resources["ink"] = new_balance
        self._emit("recycle", f"{len(recycled)} duplicate(s) recycled (+{total_ink} ink){suffix}",
                   ink_delta=total_ink, detail={"new_balance": new_balance, "failed": failed})
        self._push_resources()
        self._push_progress()
        # We just earned ink and there are no packs left -> wake the opener so it tries the
        # purchase RIGHT AWAY (real coordination between parallel tasks).
        if total_ink and self.resources.get("total_available", 0) <= 0:
            self._wake_opener()
        return len(recycled)
