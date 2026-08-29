"""Marketplace logic (phase 2) — pure functions, testable without network.

Main requested goal: permanently keep the maximum number of active listings
(5) to maximize parallel acquisitions ("don't waste time").

Two levers:
  * plan_listings     — which listings to CREATE to fill the free slots;
  * plan_fulfillments — which listings of others to FULFILL to acquire my missing cards.

Rarity rule "I give (offered) -> I receive (wanted)": see strategy.py.
We always offer the card of the LOWEST eligible rarity (preserves value).
"""
from __future__ import annotations

from .strategy import offer_rarities_for_target


def _pick_offer(target_rarity: str, avail: dict, used: dict) -> dict | None:
    """Picks the duplicate to offer in order to target a card of `target_rarity`:
    the lowest eligible rarity for which I still have a copy available.
    `avail`: {card_id: {rarity, series_id, avail}}; `used`: {card_id: already committed this round}.
    """
    for rar in offer_rarities_for_target(target_rarity):          # low -> high
        for cid in sorted(avail):                                  # deterministic
            info = avail[cid]
            if info["rarity"] == rar and info["avail"] - used.get(cid, 0) > 0:
                return {"card_id": cid, "series_id": info["series_id"]}
    return None


def plan_listings(missing_by_series: dict[str, list[dict]], dups: list[dict],
                  committed_by_type: dict[str, int], existing_wanted: set[str],
                  n_needed: int, progress: list[dict],
                  prefer_near_completion: bool = True) -> list[dict]:
    """Returns up to `n_needed` listings to create:
        [{offered_type, offered_series, wanted_card, wanted_series}]

    Priority to series near completion (targeted acquisition when few cards remain).

    INVARIANT (user rule): we only offer cards owned in multiple copies, always
    keeping >= 1 copy. -> available = quantity - 1 - already_committed
    (`dups` only contains cards with quantity >= 2; cf. Database.all_duplicates).
    """
    if n_needed <= 0:
        return []

    # Availability to offer, per card type.
    avail: dict[str, dict] = {}
    for d in dups:
        a = d["quantity"] - 1 - committed_by_type.get(d["card_id"], 0)
        if a > 0:
            avail[d["card_id"]] = {"rarity": d["rarity"], "series_id": d["series_id"], "avail": a}

    order = [p for p in progress if p["missing"] > 0]
    if prefer_near_completion:
        order.sort(key=lambda p: p["missing"])        # fewest missing first

    plans: list[dict] = []
    used: dict[str, int] = {}
    wanted_now = set(existing_wanted)
    for p in order:
        sid = p["series_id"]
        for m in missing_by_series.get(sid, []):
            if len(plans) >= n_needed:
                return plans
            if m["card_id"] in wanted_now:
                continue
            offer = _pick_offer(m["rarity"], avail, used)
            if not offer:
                continue
            plans.append({"offered_type": offer["card_id"], "offered_series": offer["series_id"],
                          "wanted_card": m["card_id"], "wanted_series": sid})
            used[offer["card_id"]] = used.get(offer["card_id"], 0) + 1
            wanted_now.add(m["card_id"])
        if len(plans) >= n_needed:
            break
    return plans


def plan_fulfillments(browse_listings: list[dict], my_missing: set[str],
                      spare_by_type: dict[str, int], max_n: int) -> list[dict]:
    """Listings of others to fulfill. Returns [{listing_id, gain_card, give_card}].

    INVARIANT (user rule) — we fulfill a listing ONLY if BOTH conditions hold:
      (a) I own the REQUESTED card as a duplicate  -> `spare_by_type[wanted] >= 1`
          (spare = quantity - 1, from all_duplicates: quantity >= 2 -> I keep 1 copy);
      (b) I do NOT already own the OFFERED card     -> `offered in my_missing`.
    `reserved`/`gained` prevent committing the same copy twice / gaining a duplicate.
    """
    out: list[dict] = []
    reserved: dict[str, int] = {}
    gained: set[str] = set()
    for l in browse_listings:
        if len(out) >= max_n:
            break
        offered = l.get("offered_card_type")      # what I would gain
        wanted = l.get("wanted_card_id")           # what I must give
        lid = l.get("id")
        if not (offered and wanted and lid):
            continue
        if offered not in my_missing or offered in gained:
            continue
        if spare_by_type.get(wanted, 0) - reserved.get(wanted, 0) <= 0:
            continue
        out.append({"listing_id": lid, "gain_card": offered, "give_card": wanted})
        reserved[wanted] = reserved.get(wanted, 0) + 1
        gained.add(offered)
    return out
