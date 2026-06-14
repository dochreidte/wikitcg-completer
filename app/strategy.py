"""Logique de stratégie.

MVP utilisé aujourd'hui :
  * select_target_series  — quelle série ouvrir en priorité ;
  * plan_recycle          — quels exemplaires recycler (en préservant la matière d'échange).

Phase 2 (échafaudage, non câblé dans le moteur MVP) :
  * expected_packs_for_card / should_trade_series — bascule dynamique boosters ↔ échanges,
    sans seuil codé en dur : on compare le coût ESPÉRÉ en packs au coût/dispo d'un échange.
"""
from __future__ import annotations

# Rareté croissante (du plus commun au plus rare), déduite des sets (C80/UC50/R30/SR20/SSR10/UR7/LR3)
RARITY_ORDER = ["C", "UC", "R", "SR", "SSR", "UR", "LR"]
RARITY_RANK = {r: i for i, r in enumerate(RARITY_ORDER)}
# Du plus rare au plus commun (affichage UI / breakdown par rareté).
RARITY_ORDER_DESC = list(reversed(RARITY_ORDER))

# Table d'échange — sémantique « je DONNE (offered) -> je REÇOIS (wanted) ».
# Pour chaque rareté offerte, raretés que l'on peut demander (égale ou descendante) :
TRADE_GIVE_TO_GET = {
    "LR": {"LR"},
    "UR": {"UR"},
    "SSR": {"SSR"},
    "SR": {"SR", "R"},
    "R": {"R", "UC"},
    "UC": {"UC", "C"},
    # "C" : ne peut PAS être offert (les commons sont reçus, ou recyclés).
}

# Raretés qui peuvent être OFFERTES en échange = clés de la table ci-dessus (toutes sauf C).
TRADEABLE_RARITIES = set(TRADE_GIVE_TO_GET)


def offer_rarities_for_target(target_rarity: str) -> list[str]:
    """Raretés que je peux OFFRIR pour acquérir une carte de `target_rarity`,
    triées de la plus faible à la plus forte (pour préserver les cartes de valeur)."""
    candidates = [give for give, gets in TRADE_GIVE_TO_GET.items() if target_rarity in gets]
    return sorted(candidates, key=lambda r: RARITY_RANK[r])


# --------------------------------------------------------------------------- #
#  MVP : choix de la série à ouvrir
# --------------------------------------------------------------------------- #
def select_target_series(progress: list[dict]) -> dict | None:
    """Priorise les séries incomplètes où il manque le PLUS de cartes
    (les boosters y sont statistiquement les plus rentables).
    `progress` = sortie de Database.progress_view().
    """
    incomplete = [p for p in progress if p["total"] and p["missing"] > 0]
    if not incomplete:
        return None
    return max(incomplete, key=lambda p: (p["missing"], -p["pct"]))


# --------------------------------------------------------------------------- #
#  MVP : politique de recyclage
# --------------------------------------------------------------------------- #
def plan_recycle(duplicates: list[dict], keep_spares: dict[str, int]) -> dict[str, int]:
    """À partir des doublons d'une série, renvoie {card_id: nb_a_recycler}.

    Surplus recyclable = quantity - 1 (exemplaire de collection) - keep_spares[rarity].
    On garde donc une réserve par rareté pour les échanges (phase 2).
    `duplicates` = lignes [{card_id, rarity, quantity}].
    """
    plan: dict[str, int] = {}
    for d in duplicates:
        keep = keep_spares.get(d["rarity"], 0)
        recyclable = d["quantity"] - 1 - keep
        if recyclable > 0:
            plan[d["card_id"]] = recyclable
    return plan


# --------------------------------------------------------------------------- #
#  Phase 2 (échafaudage) : bascule boosters <-> échanges, sans seuil fixe
# --------------------------------------------------------------------------- #
# Taux par défaut (fallback) si on n'a pas encore assez de données empiriques.
# Calibrés grossièrement sur la composition d'un set ; seront remplacés par les
# taux réellement observés via Database.empirical_pull_rates().
_FALLBACK_RATES = {"C": 0.40, "UC": 0.25, "R": 0.15, "SR": 0.10, "SSR": 0.06, "UR": 0.03, "LR": 0.01}
CARDS_PER_PACK = 5


def expected_packs_for_card(rarity: str, missing_of_rarity: int, total_of_rarity: int,
                            rates: dict[str, float] | None = None) -> float:
    """Nombre espéré de boosters pour tirer UNE carte précise manquante de cette rareté.

    Modèle simple : P(obtenir une carte voulue donnée dans un slot)
      = P(rareté) * (manquantes_de_cette_rareté / total_de_cette_rareté) / total_de_cette_rareté
    -> à mesure qu'il reste peu de manquantes, l'espérance explose (collectionneur de coupons),
       ce qui fait émerger naturellement la bascule vers l'échange ciblé.
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
    """Heuristique de bascule : si le coût espéré médian (en packs) des cartes encore
    manquantes dépasse l'équivalent-packs d'un échange, on privilégie l'échange.
    `trade_cost_packs_equiv` = coût d'opportunité d'un échange exprimé en « packs ».
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
