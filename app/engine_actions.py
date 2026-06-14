"""Actions du moteur (mixin de `Engine`) : ouverture de packs, pack mystery, recyclage.

Mêlé à `Engine` : accès à self.client / db / settings / resources / _emit / _push_* /
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
        # L'ouverture peut renvoyer 200 avec un champ {error} (ex. cooldown du pack mystery,
        # plus de packs…). On le traite comme un échec pour ne PAS décrémenter à tort.
        if isinstance(res, dict) and res.get("error"):
            raise ApiError(f"Ouverture refusée : {res['error']}", body=str(res.get("error")))
        cards = res.get("cards", [])
        # méta série (nom + couleurs) si disponibles
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
        # Compte de packs : on préfère le chiffre renvoyé par le serveur (fiable) ;
        # à défaut, on décrémente localement (optimiste).
        if res.get("totalAvailable") is not None:
            self.resources["total_available"] = res["totalAvailable"]
            self.resources["free_packs"] = res.get("freePacks", self.resources.get("free_packs", 0))
        else:
            self.resources["free_packs"] = max(self.resources.get("free_packs", 1) - 1, 0)
            self.resources["total_available"] = max(self.resources.get("total_available", 1) - 1, 0)
        self.opened_total += 1   # suivi multi-comptes : a-t-on ouvert quelque chose cette session ?
        self._emit(kind, f"{label} ouvert : {len(new_cards)} nouvelle(s), {len(dup_cards)} doublon(s)",
                   series_id=series_id, detail={"new": new_cards, "dup": dup_cards,
                                                "xp": res.get("xpEarned"), "streak": res.get("streak")})
        # Suivi : met en avant les cartes RARES tirées (SSR et plus) et les montées de niveau.
        rares = [c for c in new_cards if c["rarity"] in ("SSR", "UR", "LR")]
        for c in rares:
            self._emit("rare", f"✨ Carte rare : {c['rarity']} {c.get('title') or c['id']}",
                       series_id=series_id, detail=c)
        lvl = res.get("levelUp")
        if lvl:
            new_lvl = lvl.get("level") if isinstance(lvl, dict) else lvl
            self._emit("level", f"⬆️ Niveau {new_lvl} atteint !", detail={"levelUp": lvl})
        self._push_resources()
        self._push_progress()
        # Annule sans tarder toute annonce devenue inutile : j'ai tiré la carte qu'elle visait.
        if self._my_listings and new_cards:
            await self._cancel_obsolete_listings({c["id"] for c in new_cards})

    async def _cancel_obsolete_listings(self, acquired_ids: set[str]) -> None:
        """Annule les annonces actives dont la carte VOULUE vient d'être obtenue."""
        remaining: list[dict] = []
        for l in self._my_listings:
            if l.get("wanted_card_id") in acquired_ids:
                try:
                    await self.client.marketplace_cancel(l["id"])
                    self._emit("listing", f"Annonce annulée : {l['wanted_card_id']} obtenue dans un pack",
                               detail=l)
                except ApiError as exc:
                    self._emit("listing", f"Annulation refusée : {exc}", level="warn", detail=l)
                    remaining.append(l)
            else:
                remaining.append(l)
        self._my_listings = remaining

    async def _try_open_mystery(self) -> bool:
        """Ouvre le pack 'mystery' (premium, ~1×/6h) via /api/packs/open seriesId=mystery —
        comme un pack normal (il consomme un pack dispo, 0 encre). Sur cooldown/refus, on
        re-tente plus tard sans spammer. L'échéance est persistée (survit au redémarrage)."""
        interval = float(self.settings.engine.get("mystery_interval_hours", 6.0)) * 3600
        try:
            await self.open_one("mystery", label="Pack mystery", kind="mystery")
        except ApiError as exc:
            self._mystery_due_at = time.time() + 1800   # cooldown/refus : nouvel essai dans ~30 min
            self.db.set_kv("mystery_due_at", str(self._mystery_due_at))
            self._emit("mystery", f"Pack mystery indisponible ({exc}) — nouvel essai plus tard.",
                       level="warn")
            return False
        self._mystery_due_at = time.time() + interval
        self.db.set_kv("mystery_due_at", str(self._mystery_due_at))
        return True

    async def recycle_pass(self, target_ink: int | None = None) -> int:
        """Recyclage GLOBAL : récupère les doublons via /api/cards/duplicates,
        applique la politique keep_spares, recycle le surplus. Renvoie le nb recyclé.

        Chaque doublon = {card_id, series_id, rarity, copies, pull_ids:[…]} ; on recycle
        les `copies - 1 - réserve` premiers exemplaires (pull_ids). En cas de réponse non
        interprétable (aucun exemplaire identifié) -> avertissement, 0 recyclage.

        Si `target_ink` est fourni (recyclage « à la demande »), on ne recycle QUE le minimum
        nécessaire pour atteindre cette encre, en sacrifiant les exemplaires les MOINS
        précieux d'abord — pour préserver les cartes de valeur (matière d'échange).
        """
        # Quota JOURNALIER de recyclage atteint récemment (429) -> on suspend : inutile
        # d'appeler /duplicates ni /recycle, tout renverrait 429. L'ouverture continue.
        quota_until = getattr(self, "_recycle_quota_until", 0.0)
        if quota_until > time.time():
            log.debug("Recyclage en pause (quota journalier) — reprise dans ~%d min.",
                      int((quota_until - time.time()) / 60) + 1)
            return 0
        dups = await self.client.get_duplicates(self.settings.recycle)
        if not dups:
            return 0
        if all(not d.get("pull_ids") for d in dups):
            if not self._recycle_warned:
                self._recycle_warned = True
                self._emit("recycle", "Réponse de /api/cards/duplicates non reconnue : "
                           "recyclage suspendu (colle un exemple pour finaliser le parsing).",
                           level="warn")
            return 0

        keep = self.settings.engine.get("keep_spares", {})
        values = self.settings.recycle.get("values", {})
        now = time.time()
        mkt_on = bool(self.settings.marketplace.get("enabled"))
        tradeable = {"UC", "R", "SR", "SSR", "UR", "LR"}
        skip_rarities = set(self.settings.engine.get("recycle_skip_rarities", []))
        # Réserve : "fixed" = keep_spares par carte (+plancher marketplace) ; "missing" = garder,
        # PAR RARETÉ, autant de doublons qu'il manque de cartes de cette rareté (matière d'échange
        # pour acquérir les manquantes). Ex. 15 LR en double, 3 manquantes -> on en recycle 12.
        reserve_mode = self.settings.engine.get("recycle_reserve_mode", "fixed")
        missing_by_rar = self.db.missing_by_rarity() if reserve_mode == "missing" else {}

        # Pour chaque TYPE : `surplus` = nb d'exemplaires recyclables de cette carte (on garde
        # toujours 1 exemplaire de collection). On retient TOUS les pullIds (hors cooldown) pour
        # pouvoir tenter un AUTRE exemplaire si l'un est verrouillé (500).
        groups: list[dict] = []
        extras_by_rar: dict[str, int] = {}
        skipped_cooldown = 0
        for d in dups:
            rarity = d.get("rarity") or ""
            if rarity in skip_rarities:   # rareté exclue (ex. LR) -> on ne recycle jamais
                continue
            copies = d.get("copies", len(d["pull_ids"]))
            if reserve_mode == "missing":
                surplus = copies - 1                       # tous les extras ; réserve appliquée par rareté
            else:
                reserve = keep.get(rarity, 0)
                if mkt_on and rarity in tradeable:
                    reserve = max(reserve, 1)
                surplus = copies - 1 - reserve
            if surplus <= 0:
                continue
            pulls = []
            for pid in d["pull_ids"]:
                if pid in self._recycle_done:              # déjà recyclé/épuisé cette session
                    continue                                # (liste /duplicates en retard) -> on ignore
                if self._recycle_skip.get(pid, 0) > now:   # en cooldown après un 500
                    skipped_cooldown += 1
                else:
                    pulls.append(pid)
            if pulls:
                extras_by_rar[rarity] = extras_by_rar.get(rarity, 0) + surplus
                groups.append({"card_id": d.get("card_id"), "rarity": rarity,
                               "series_id": d.get("series_id"), "surplus": surplus, "pulls": pulls})

        # Mode "missing" : budget recyclable PAR RARETÉ = total des extras - nb de manquantes.
        rarity_budget = {}
        if reserve_mode == "missing":
            for r, extras in extras_by_rar.items():
                rarity_budget[r] = max(extras - missing_by_rar.get(r, 0), 0)

        if not groups:
            if skipped_cooldown:
                log.debug("Recyclage : %d exemplaire(s) en cooldown 500, réessai plus tard.",
                          skipped_cooldown)
            else:
                log.debug("Recyclage : aucun surplus au-delà de la réserve (%d type(s))", len(dups))
            return 0
        # Ordre de recyclage par rareté :
        #  • "common_first" (défaut) : on sacrifie les moins rares d'abord -> préserve les cartes
        #    de valeur (matière d'échange) ;
        #  • "rare_first" (farm pur) : on recycle les plus rares d'abord -> encre MAXIMALE extraite
        #    dans la limite du quota journalier (~200 recyclages/j) puisque LR/UR rapportent le plus.
        rare_first = self.settings.engine.get("recycle_priority", "common_first") == "rare_first"
        groups.sort(key=lambda g: strategy.RARITY_RANK.get(g["rarity"], 99), reverse=rare_first)
        cap = int(self.settings.recycle.get("max_per_call", 20))
        if target_ink is not None:
            log.info("Recyclage à la demande : viser %d encre (actuel %d).",
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
            if reserve_mode == "missing":   # plafonné par le budget restant de la rareté
                card_cap = min(card_cap, rarity_budget.get(r, 0) - rarity_used.get(r, 0))
            if card_cap <= 0:
                continue
            got = 0   # nb recyclé pour CE type (≤ card_cap)
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
                        # 429 = QUOTA JOURNALIER de recyclage atteint (~200/jour) : toutes les
                        # cartes renverraient 429. On met le recyclage en pause longue (au lieu de
                        # marteler carte par carte) ; l'ouverture, elle, continue en parallèle.
                        cd = float(self.settings.engine.get("recycle_quota_cooldown_minutes", 60.0)) * 60
                        self._recycle_quota_until = time.time() + cd
                        self.db.set_kv("recycle_quota_until", str(self._recycle_quota_until))
                        self._emit("recycle", "Quota journalier de recyclage atteint — recyclage en "
                                   f"pause {int(cd / 60)} min (l'ouverture continue).", level="warn")
                        stop = True
                        break
                    body = getattr(exc, "body", "") or ""
                    if getattr(exc, "status", None) == 400 and (
                            "cards_not_found" in body or "cannot_recycle_last_copy" in body):
                        # 400 BÉNIN : exemplaire déjà recyclé (liste /duplicates en retard) ou il ne
                        # reste qu'1 copie (compte périmé). Ce n'est PAS une erreur : on marque le
                        # pullId comme « terminé » (plus jamais re-tenté) et on passe, sans alarme.
                        self._recycle_done.add(pid)
                        reason = "cards_not_found" if "cards_not_found" in body else "cannot_recycle_last_copy"
                        log.debug("Recyclage : exemplaire %s (%s) ignoré (400 %s — déjà recyclé / "
                                  "dernière copie).", g["card_id"], g["rarity"], reason)
                        continue
                    # 500 (ou autre 4xx inattendu) : exemplaire non recyclable (verrouillé côté
                    # serveur) → cooldown croissant persisté ; on tente l'exemplaire SUIVANT.
                    now_f = time.time()
                    retry_at = self.db.mark_recycle_failure(pid, g["card_id"], g["rarity"],
                                                            retry_after, now_f)
                    self._recycle_skip[pid] = retry_at
                    failed += 1
                    log.debug("Recyclage : exemplaire %s (%s) non recyclable, réessai à +%d min — %s",
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
                self._recycle_done.add(pid)   # ne plus jamais le re-soumettre (liste /duplicates en retard)
                recycled.append(pid)
                got += 1
                rarity_used[r] = rarity_used.get(r, 0) + 1

        if not recycled:
            if failed:
                self._emit("recycle", f"{failed} carte(s) en erreur 500 — réessai dans "
                           f"{int(retry_after/60)} min.", level="warn")
            return 0
        self.recycled_total += len(recycled)   # suivi multi-comptes
        suffix = f" ({failed} en erreur, réessai +{int(retry_after/60)} min)" if failed else ""
        if new_balance is not None:
            self.resources["ink"] = new_balance
        self._emit("recycle", f"{len(recycled)} doublon(s) recyclé(s) (+{total_ink} encre){suffix}",
                   ink_delta=total_ink, detail={"new_balance": new_balance, "failed": failed})
        self._push_resources()
        self._push_progress()
        # On vient de gagner de l'encre et il n'y a plus de packs → réveiller l'ouverture pour
        # qu'elle tente l'achat TOUT DE SUITE (vraie coordination entre tâches parallèles).
        if total_ink and self.resources.get("total_available", 0) <= 0:
            self._wake_opener()
        return len(recycled)
