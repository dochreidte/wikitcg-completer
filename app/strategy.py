"""Strategy logic.

MVP used today:
  * select_target_series  — which series to open first;
  * plan_recycle          — which copies to recycle (while preserving trade material).

Phase 2 (scaffolding, not wired into the MVP engine):
  * expected_packs_for_card / should_trade_series — dynamic booster <-> trade switch,
    with no hard-coded threshold: compare the EXPECTED pack cost against a trade's cost/availability.
"""
from __future__ import annotations

# Rarity in ascending order (most common to rarest), derived from the sets (C80/UC50/R30/SR20/SSR10/UR7/LR3)
RARITY_ORDER = ["C", "UC", "R", "SR", "SSR", "UR", "LR"]
RARITY_RANK = {r: i for i, r in enumerate(RARITY_ORDER)}
# Rarest to most common (UI display / per-rarity breakdown).
RARITY_ORDER_DESC = list(reversed(RARITY_ORDER))

# Trade table — semantics "I GIVE (offered) -> I RECEIVE (wanted)".
# For each offered rarity, the rarities one may request (equal or lower):
TRADE_GIVE_TO_GET = {
    "LR": {"LR"},
    "UR": {"UR"},
    "SSR": {"SSR"},
    "SR": {"SR", "R"},
    "R": {"R", "UC"},
    "UC": {"UC", "C"},
    # "C": CANNOT be offered (commons are received, or recycled).
}

# Rarities that can be OFFERED in a trade = keys of the table above (all but C).
TRADEABLE_RARITIES = set(TRADE_GIVE_TO_GET)


def offer_rarities_for_target(target_rarity: str) -> list[str]:
    """Rarities I can OFFER to acquire a card of `target_rarity`,
    sorted from lowest to highest (to preserve valuable cards)."""
    candidates = [give for give, gets in TRADE_GIVE_TO_GET.items() if target_rarity in gets]
    return sorted(candidates, key=lambda r: RARITY_RANK[r])


# --------------------------------------------------------------------------- #
#  MVP: choosing which series to open
# --------------------------------------------------------------------------- #
def select_target_series(progress: list[dict]) -> dict | None:
    """Prioritizes the incomplete series missing the MOST cards
    (boosters are statistically the most profitable there).
    `progress` = output of Database.progress_view().
    """
    incomplete = [p for p in progress if p["total"] and p["missing"] > 0]
    if not incomplete:
        return None
    return max(incomplete, key=lambda p: (p["missing"], -p["pct"]))


# --------------------------------------------------------------------------- #
#  MVP: recycling policy
# --------------------------------------------------------------------------- #
def plan_recycle(duplicates: list[dict], keep_spares: dict[str, int]) -> dict[str, int]:
    """From a series' duplicates, returns {card_id: count_to_recycle}.

    Recyclable surplus = quantity - 1 (collection copy) - keep_spares[rarity].
    So we keep a reserve per rarity for trades (phase 2).
    `duplicates` = rows [{card_id, rarity, quantity}].
    """
    plan: dict[str, int] = {}
    for d in duplicates:
        keep = keep_spares.get(d["rarity"], 0)
        recyclable = d["quantity"] - 1 - keep
        if recyclable > 0:
            plan[d["card_id"]] = recyclable
    return plan


# --------------------------------------------------------------------------- #
#  Phase 2 (scaffolding): booster <-> trade switch, with no fixed threshold
# --------------------------------------------------------------------------- #
# Default (fallback) rates when we don't yet have enough empirical data.
# Roughly calibrated on a set's composition; will be replaced by the
# actually observed rates via Database.empirical_pull_rates().
_FALLBACK_RATES = {"C": 0.40, "UC": 0.25, "R": 0.15, "SR": 0.10, "SSR": 0.06, "UR": 0.03, "LR": 0.01}
CARDS_PER_PACK = 5


def expected_packs_for_card(rarity: str, missing_of_rarity: int, total_of_rarity: int,
                            rates: dict[str, float] | None = None) -> float:
    """Expected number of boosters to pull ONE specific missing card of this rarity.

    Simple model: P(getting a given wanted card in a slot)
      = P(rarity) * (missing_of_this_rarity / total_of_this_rarity) / total_of_this_rarity
    -> as fewer cards remain missing, the expectation blows up (coupon collector),
       which naturally surfaces the switch to targeted trading.
    """
    rates = rates or _FALLBACK_RATES
    p_rarity = rates.get(rarity, 0.02)
    if total_of_rarity <= 0 or missing_of_rarity <= 0 or p_rarity <= 0:
        return float("inf")
    p_specific_card = p_rarity * (missing_of_rarity / total_of_rarity) / total_of_rarity
    p_per_pack = 1 - (1 - p_specific_card) ** CARDS_PER_PACK
    return float("inf") if p_per_pack <= 0 else 1.0 / p_per_pack


def should_trade_series(missing_by_rarity: dict[str, int], total_by_rarity: dict[str, int],
                        rates: dict[str, float] | None = None,
                        trade_cost_packs_equiv: float = 6.0) -> bool:
    """Switch heuristic: if the median expected cost (in packs) of the still-missing
    cards exceeds the pack-equivalent of a trade, we favor trading.
    `trade_cost_packs_equiv` = opportunity cost of a trade expressed in "packs".
    """
    costs = []
    for rar, miss in missing_by_rarity.items():
        if miss > 0:
            costs.append(expected_packs_for_card(rar, miss, total_by_rarity.get(rar, miss), rates))
    if not costs:
        return False
    costs.sort()
    median = costs[len(costs) // 2]
    return median > trade_cost_packs_equiv
