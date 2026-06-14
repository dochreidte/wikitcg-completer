"""Volet marketplace du moteur (mixin de `Engine`).

Contrats vérifiés (frontend du site + captures HAR + test live create→cancel) :
  create  : POST /api/marketplace/create {offeredCardTypeId, offeredSeriesId,
            wantedCardId, wantedSeriesId}  → 201 {success}
  cancel  : POST /api/marketplace/{id}/cancel → 200 {success}
  fulfill : POST /api/marketplace/{id}/fulfill (donne une carte à un autre joueur — irréversible).
Désactivé par défaut : engage de vraies cartes et touche d'autres joueurs réels.
Mode `dry_run` : journalise ce qui SERAIT fait, sans aucune écriture.
"""
from __future__ import annotations

import json
import logging
import time

from .api_client import ApiError
from . import marketplace

log = logging.getLogger("wikitcg.engine")


class MarketMixin:
    """Méthodes marketplace, mêlées à `Engine` (accès via self.client/db/settings/_emit…)."""

    async def marketplace_pass(self) -> int:
        """Maintient le pool d'annonces. Renvoie le nombre d'ACTIONS effectuées
        (annulations + honneurs + créations) — sert au backoff de la boucle marketplace."""
        cfg = self.settings.marketplace
        if not cfg.get("enabled"):
            return 0
        dry = bool(cfg.get("dry_run"))      # mode validation : on journalise sans écrire
        cancels = fulfills = creates = 0
        mine = await self.client.marketplace_mine()
        listings = mine.get("listings") or []

        # 0a) Annonces HONORÉES : j'ai reçu la carte voulue. On la crédite à l'inventaire (une
        #     seule fois, suivi persistant) pour NE PAS recréer l'offre ensuite (bug « recrée une
        #     annonce déjà honorée »). On décrémente aussi l'exemplaire offert (donné).
        seen = set(json.loads(self.db.get_kv("fulfilled_seen", "[]") or "[]"))
        changed = False
        for l in listings:
            if (l.get("status") == "fulfilled" or l.get("fulfilled_by")) and l["id"] not in seen:
                wc, ws = l.get("wanted_card_id"), l.get("wanted_series_id")
                if wc and ws and not self.db.owns_card(wc):
                    self.db.bump_inventory(ws, wc, l.get("wanted_rarity"))
                if l.get("offered_card_type") and l.get("offered_series"):
                    self.db.decrement_inventory(l["offered_series"], l["offered_card_type"], by=1)
                self._emit("fulfill", f"Annonce honorée par un autre joueur : j'ai reçu {wc}",
                           detail=l)
                seen.add(l["id"]); changed = True
        if changed:
            self.db.set_kv("fulfilled_seen", json.dumps(list(seen)[-1000:]))  # borne la taille

        active = [l for l in listings if l.get("status") == "active"]

        # 0b) Balayage des annonces actives : annuler celles devenues INUTILES (carte voulue déjà
        #     obtenue) OU TROP ANCIENNES (> max_listing_age_hours) — elles seront recréées fraîches.
        now_ms = time.time() * 1000
        max_age_ms = float(cfg.get("max_listing_age_hours", 24)) * 3600 * 1000
        still_active = []
        for l in active:
            wc = l.get("wanted_card_id")
            owned = wc and self.db.owns_card(wc)
            too_old = max_age_ms > 0 and (now_ms - (l.get("created_at") or now_ms)) > max_age_ms
            if owned or too_old:
                reason = "carte déjà obtenue" if owned else "annonce > 1 j (renouvellement)"
                if dry:
                    self._emit("listing", f"[simulé] annulerait : {reason}", detail=l)
                    continue
                try:
                    await self.client.marketplace_cancel(l["id"])
                    self._emit("listing", f"Annonce annulée : {reason}", detail=l)
                    cancels += 1
                    continue
                except ApiError as exc:
                    self._emit("listing", f"Annulation refusée : {exc}", level="warn", detail=l)
            still_active.append(l)
        active = still_active

        # Cache pour l'annulation immédiate côté ouverture (open_one).
        self._my_listings = [{"id": l["id"], "wanted_card_id": l.get("wanted_card_id")}
                             for l in active]
        self.resources["listings_active"] = len(active)
        self._push_resources()

        # 1) Honorer les annonces utiles (acquisition instantanée), si activé.
        if cfg.get("fulfill_others"):
            fulfills = await self._fulfill_useful(cfg)

        # 2) Re-remplir jusqu'à max_listings annonces actives.
        max_listings = int(cfg.get("max_listings", 5))
        n_needed = max_listings - len(active)
        if n_needed > 0:
            committed: dict[str, int] = {}
            for l in active:
                t = l.get("offered_card_type")
                committed[t] = committed.get(t, 0) + 1
            existing_wanted = {l.get("wanted_card_id") for l in active}
            progress = self.db.progress_view()
            # Focalisation : ne cibler QUE les séries proches de la fin (≤ N cartes manquantes).
            near = int(cfg.get("near_completion_max_missing", 0))
            missing_by_series = {p["series_id"]: self.db.missing_cards(p["series_id"])
                                 for p in progress
                                 if p["missing"] > 0 and (near <= 0 or p["missing"] <= near)}
            dups = self.db.all_duplicates()
            plans = marketplace.plan_listings(missing_by_series, dups, committed, existing_wanted,
                                              n_needed, progress, cfg.get("prefer_near_completion", True))
            # On offre un TYPE de carte (le serveur engage un exemplaire). Contrat vérifié.
            for pl in plans:
                if dry:
                    self._emit("listing",
                               f"[simulé] créerait : j'offre {pl['offered_type']} pour {pl['wanted_card']}",
                               series_id=pl["wanted_series"], detail=pl)
                    continue
                try:
                    await self.client.marketplace_create(pl["offered_type"], pl["offered_series"],
                                                         pl["wanted_card"], pl["wanted_series"])
                    self._emit("listing",
                               f"Annonce créée : j'offre {pl['offered_type']} pour {pl['wanted_card']}",
                               series_id=pl["wanted_series"], detail=pl)
                    creates += 1
                except ApiError as exc:
                    self._emit("listing", f"Création d'annonce refusée : {exc}", level="warn", detail=pl)
                    break

        actions = cancels + fulfills + creates
        # Résumé de passe (suivi) — seulement s'il s'est passé quelque chose, sinon silencieux.
        if actions:
            self._emit("market", f"Marketplace : {len(active)} active(s) · "
                       f"{creates} créée(s), {fulfills} honorée(s), {cancels} annulée(s)",
                       detail={"active": len(active), "created": creates,
                               "fulfilled": fulfills, "cancelled": cancels})
        return actions

    async def _fulfill_useful(self, cfg: dict) -> int:
        """Honore les annonces d'autrui utiles. Renvoie le nombre d'échanges réalisés.

        Invariant (règle utilisateur) : on honore SEULEMENT si (a) je possède la carte demandée
        en double ET (b) je ne possède pas déjà la carte offerte (cf. marketplace.plan_fulfillments)."""
        browse = await self.client.marketplace_browse()
        listings = browse.get("listings") or []
        my_missing: set[str] = set()
        for p in self.db.progress_view():
            if p["missing"] > 0:
                my_missing.update(m["card_id"] for m in self.db.missing_cards(p["series_id"]))
        spare_by_type = {d["card_id"]: d["quantity"] - 1 for d in self.db.all_duplicates()}
        deals = marketplace.plan_fulfillments(listings, my_missing, spare_by_type,
                                              int(cfg.get("fulfill_per_pass", 3)))
        dry = bool(cfg.get("dry_run"))
        done = 0
        for deal in deals:
            if dry:
                self._emit("fulfill",
                           f"[simulé] honorerait : gagner {deal['gain_card']} contre {deal['give_card']}",
                           detail=deal)
                continue
            try:
                await self.client.marketplace_fulfill(deal["listing_id"])
                self._emit("fulfill",
                           f"Annonce honorée : je gagne {deal['gain_card']} contre {deal['give_card']}",
                           detail=deal)
                done += 1
            except ApiError as exc:
                self._emit("fulfill", f"Échec d'honneur d'annonce : {exc}", level="warn", detail=deal)
        return done
