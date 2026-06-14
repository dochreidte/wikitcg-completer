"""Logique marketplace (phase 2) — fonctions pures, testables sans réseau.

Objectif principal demandé : garder en permanence le maximum d'annonces actives
(5) pour maximiser les acquisitions en parallèle ("ne pas perdre de temps").

Deux leviers :
  * plan_listings   — quelles annonces CRÉER pour remplir les emplacements libres ;
  * plan_fulfillments — quelles annonces d'autrui HONORER pour acquérir mes manquantes.

Règle de rareté « je donne (offered) -> je reçois (wanted) » : voir strategy.py.
On offre toujours la carte de la rareté la PLUS FAIBLE éligible (préserve la valeur).
"""
from __future__ import annotations

from .strategy import offer_rarities_for_target


def _pick_offer(target_rarity: str, avail: dict, used: dict) -> dict | None:
    """Choisit le doublon à offrir pour viser une carte de `target_rarity` :
    la rareté éligible la plus faible dont il me reste un exemplaire disponible.
    `avail` : {card_id: {rarity, series_id, avail}} ; `used` : {card_id: déjà engagé ce tour}.
    """
    for rar in offer_rarities_for_target(target_rarity):          # faible -> fort
        for cid in sorted(avail):                                  # déterministe
            info = avail[cid]
            if info["rarity"] == rar and info["avail"] - used.get(cid, 0) > 0:
                return {"card_id": cid, "series_id": info["series_id"]}
    return None


def plan_listings(missing_by_series: dict[str, list[dict]], dups: list[dict],
                  committed_by_type: dict[str, int], existing_wanted: set[str],
                  n_needed: int, progress: list[dict],
                  prefer_near_completion: bool = True) -> list[dict]:
    """Renvoie jusqu'à `n_needed` annonces à créer :
        [{offered_type, offered_series, wanted_card, wanted_series}]

    Priorité aux séries proches de la complétion (acquisition ciblée quand il reste
    peu de cartes).

    INVARIANT (règle utilisateur) : on n'offre QUE des cartes possédées en plusieurs
    exemplaires, en gardant toujours ≥ 1 exemplaire. → disponible = quantity - 1 - déjà_engagé
    (`dups` ne contient que des cartes quantity ≥ 2 ; cf. Database.all_duplicates).
    """
    if n_needed <= 0:
        return []

    # Disponibilités à offrir, par type de carte.
    avail: dict[str, dict] = {}
    for d in dups:
        a = d["quantity"] - 1 - committed_by_type.get(d["card_id"], 0)
        if a > 0:
            avail[d["card_id"]] = {"rarity": d["rarity"], "series_id": d["series_id"], "avail": a}

    order = [p for p in progress if p["missing"] > 0]
    if prefer_near_completion:
        order.sort(key=lambda p: p["missing"])        # le moins de manquantes d'abord

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
    """Annonces d'autrui à honorer. Renvoie [{listing_id, gain_card, give_card}].

    INVARIANT (règle utilisateur) — on honore une annonce SEULEMENT si les DEUX conditions
    sont vraies :
      (a) je possède la carte DEMANDÉE en double  → `spare_by_type[wanted] ≥ 1`
          (spare = quantity - 1, issu d'all_duplicates : quantity ≥ 2 → je garde 1 exemplaire) ;
      (b) je ne possède PAS déjà la carte OFFERTE  → `offered ∈ my_missing`.
    `reserved`/`gained` évitent d'engager deux fois le même exemplaire / de gagner un doublon.
    """
    out: list[dict] = []
    reserved: dict[str, int] = {}
    gained: set[str] = set()
    for l in browse_listings:
        if len(out) >= max_n:
            break
        offered = l.get("offered_card_type")      # ce que je gagnerais
        wanted = l.get("wanted_card_id")           # ce que je dois donner
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
