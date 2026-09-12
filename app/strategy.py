"""Strategy logic: select series and plan recycling."""
from __future__ import annotations

RARITY_ORDER = ["C", "UC", "R", "SR", "SSR", "UR", "LR"]
RARITY_RANK = {r: i for i, r in enumerate(RARITY_ORDER)}
RARITY_ORDER_DESC = list(reversed(RARITY_ORDER))

TRADE_GIVE_TO_GET = {
    "LR": {"LR"},
    "UR": {"UR"},
    "SSR": {"SSR"},
    "SR": {"SR", "R"},
    "R": {"R", "UC"},
    "UC": {"UC", "C"},
}

TRADEABLE_RARITIES = set(TRADE_GIVE_TO_GET)


def offer_rarities_for_target(target_rarity: str) -> list[str]:
    candidates = [give for give, gets in TRADE_GIVE_TO_GET.items() if target_rarity in gets]
    return sorted(candidates, key=lambda r: RARITY_RANK[r])


def select_target_series(progress: list[dict]) -> dict | None:
    incomplete = [p for p in progress if p["total"] and p["missing"] > 0]
    if not incomplete:
        return None
    return max(incomplete, key=lambda p: (p["missing"], -p["pct"]))


def select_farm_series(progress: list[dict], pulls: dict[str, dict[str, int]],
                       values: dict[str, int]) -> dict | None:
    def ink_per_pull(p: dict) -> float:
        counts = pulls.get(p["series_id"], {})
        n = sum(counts.values())
        return sum(values.get(r, 0) * c for r, c in counts.items()) / n if n else 0.0
    return max(progress, key=ink_per_pull, default=None)


def plan_recycle(duplicates: list[dict], keep_spares: dict[str, int]) -> dict[str, int]:
    plan: dict[str, int] = {}
    for d in duplicates:
        keep = keep_spares.get(d["rarity"], 0)
        recyclable = d["quantity"] - 1 - keep
        if recyclable > 0:
            plan[d["card_id"]] = recyclable
    return plan
