"""Marketplace logic — pure functions for planning listings and fulfillments."""
from __future__ import annotations

from .strategy import offer_rarities_for_target


def _pick_offer(target_rarity: str, avail: dict, used: dict) -> dict | None:
    for rar in offer_rarities_for_target(target_rarity):
        for cid in sorted(avail):
            info = avail[cid]
            if info["rarity"] == rar and info["avail"] - used.get(cid, 0) > 0:
                return {"card_id": cid, "series_id": info["series_id"]}
    return None


def plan_listings(missing_by_series: dict[str, list[dict]], dups: list[dict],
                  committed_by_type: dict[str, int], existing_wanted: set[str],
                  n_needed: int, progress: list[dict],
                  prefer_near_completion: bool = True) -> list[dict]:
    if n_needed <= 0:
        return []

    avail: dict[str, dict] = {}
    for d in dups:
        a = d["quantity"] - 1 - committed_by_type.get(d["card_id"], 0)
        if a > 0:
            avail[d["card_id"]] = {"rarity": d["rarity"], "series_id": d["series_id"], "avail": a}

    order = [p for p in progress if p["missing"] > 0]
    if prefer_near_completion:
        order.sort(key=lambda p: p["missing"])

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
    out: list[dict] = []
    reserved: dict[str, int] = {}
    gained: set[str] = set()
    for l in browse_listings:
        if len(out) >= max_n:
            break
        offered = l.get("offered_card_type")
        wanted = l.get("wanted_card_id")
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
