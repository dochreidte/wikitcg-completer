"""Moteur d'automatisation (MVP) : synchronisation + ouverture auto + recyclage.

Boucle, en mode « tout automatique » :
  1. lire le statut (encre, packs gratuits, niveau) ;
  2. choisir la série incomplète où il manque le plus de cartes ;
  3. ouvrir un booster, enregistrer les tirages, mettre à jour l'inventaire ;
  4. recycler le surplus de doublons (selon la politique, si l'endpoint est configuré) ;
  5. quand les packs gratuits sont épuisés : acheter un restock avec l'encre
     (recyclage des doublons d'abord si besoin) ; sinon attendre la régénération.

Les échanges marketplace restent opt-in (phase 2, désactivés par défaut).
"""
from __future__ import annotations

import asyncio
import logging
import time

from .api_client import ApiError, AuthError, WikiTCGClient
from .db import Database
from .events import EventBus
from .engine_actions import ActionsMixin
from .engine_market import MarketMixin
from .series_seed import SERIES_SEED, DEFAULT_PRIMARY, DEFAULT_ACCENT
from . import strategy

log = logging.getLogger("wikitcg.engine")

# Niveau d'événement (_emit) -> méthode du logger ; défaut INFO.
_LOG_FN = {"warn": log.warning, "error": log.error}


def _prettify(series_id: str) -> str:
    return series_id.replace("-", " ").replace("_", " ").title()


class Engine(ActionsMixin, MarketMixin):
    def __init__(self, client: WikiTCGClient, db: Database, bus: EventBus, settings):
        self.client = client
        self.db = db
        self.bus = bus
        self.settings = settings
        self.running = False
        self.status = "idle"          # idle | syncing | running | waiting | stopped | error
        self.resources: dict = {}
        self._task: asyncio.Task | None = None
        self._recycle_warned = False
        self._opens_since_resync = 0
        self.opened_total = 0         # nb de packs ouverts depuis le démarrage (suivi multi-comptes)
        self.recycled_total = 0       # nb d'exemplaires recyclés depuis le démarrage (suivi multi-comptes)
        self._last_target = None
        self.auth_error: str | None = None   # dernier échec d'authentification (UI)
        # Cache de mes annonces actives (id + carte voulue) — sert à annuler vite une
        # annonce dès que je tire/obtiens la carte qu'elle visait. Tenu par marketplace_pass.
        self._my_listings: list[dict] = []
        # Exemplaires (pullIds) ayant renvoyé 500 au recyclage (ex. carte engagée dans un
        # deck) -> epoch jusqu'auquel on ne les retente PAS. On les re-essaie après X minutes
        # (recycle_retry_minutes) : une carte peut redevenir recyclable (sortie d'un deck…).
        # PERSISTÉ en base (table recycle_failures) -> survit aux redémarrages.
        self._recycle_skip: dict[str, float] = self.db.recycle_skips()
        # pullIds déjà traités cette session (recyclés AVEC succès, ou 400 « déjà recyclé / dernière
        # copie ») : on ne les re-soumet JAMAIS, car /api/cards/duplicates est en retard et continue
        # de les lister un moment -> sinon on retente et on récolte des 400 cards_not_found.
        self._recycle_done: set[str] = set()
        # Pack mystery (premium, ~1×/6h) : epoch du prochain essai autorisé (persisté).
        self._mystery_due_at = float(self.db.get_kv("mystery_due_at", "0") or 0)
        # Quota JOURNALIER de recyclage atteint (le serveur renvoie 429 sur /api/cards/recycle ~200/j) :
        # epoch jusqu'auquel on suspend tout recyclage (persisté). L'ouverture, elle, continue.
        self._recycle_quota_until = float(self.db.get_kv("recycle_quota_until", "0") or 0)
        # Backoffs croissants (s) : attente quand rien à faire (ouverture / marketplace).
        self._idle_backoff = 0.0
        self._mkt_backoff = 0.0
        self._last_mkt_pass = 0.0    # epoch de la dernière passe marketplace réellement exécutée
        self._last_full_sync = 0.0   # epoch du dernier full_sync (pour le re-sync périodique)
        # Réveil de la boucle d'ouverture : posé par le recyclage quand il vient de gagner de
        # l'encre alors que les packs sont vides → l'ouverture re-tente l'achat sans attendre.
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------ #
    #  Émission d'événements (persistés + poussés à l'UI)
    # ------------------------------------------------------------------ #
    def _emit(self, type_: str, message: str, *, series_id: str = "",
              level: str = "info", detail: dict | None = None, ink_delta: int = 0) -> None:
        ev = self.db.log_action(type_, message, series_id=series_id, level=level,
                                detail=detail, ink_delta=ink_delta)
        ev["kind"] = "action"
        self.bus.publish(ev)
        _LOG_FN.get(level, log.info)("%s%s", message, f" [{series_id}]" if series_id else "")

    def _set_status(self, status: str) -> None:
        self.status = status
        self.bus.publish({"kind": "status", "status": status, "running": self.running})

    def _push_resources(self) -> None:
        self.bus.publish({"kind": "resources", **self.resources})

    def _push_progress(self) -> None:
        self.bus.publish({"kind": "progress", "series": self.db.progress_view()})

    # ------------------------------------------------------------------ #
    #  Synchronisation
    # ------------------------------------------------------------------ #
    async def sync_status(self) -> dict:
        prev_total = self.resources.get("total_available")
        st = await self.client.get_status()
        self.auth_error = None   # un statut OK prouve que la session est valide
        self.resources = {
            "ink": st.get("ink", 0),
            "free_packs": st.get("freePacks", 0),
            "paid_packs": st.get("paidPacks", 0),
            "total_available": st.get("totalAvailable", st.get("freePacks", 0)),
            "level": st.get("level", 0),
            "xp": st.get("xp", 0),
            "xp_to_next": st.get("xpToNext", 0),
            "xp_progress": st.get("xpProgress"),
            "streak": st.get("streak", 0),
            "max_free_packs": st.get("maxFreePacks", 0),
            "next_regen_at": st.get("nextRegenAt"),
        }
        self.db.snapshot_resources(self.resources["ink"], self.resources["free_packs"],
                                   self.resources["paid_packs"], self.resources["level"],
                                   self.resources["xp"])
        log.debug("Statut : encre=%d packs=%d/%d niveau=%d xp=%d streak=%d",
                  self.resources["ink"], self.resources["total_available"],
                  self.resources["max_free_packs"], self.resources["level"],
                  self.resources["xp"], self.resources["streak"])
        # Suivi : signaler une régénération (le compte de packs a augmenté depuis la dernière lecture).
        new_total = self.resources["total_available"]
        if prev_total is not None and new_total > prev_total:
            self._emit("regen", f"Régénération : +{new_total - prev_total} pack(s) "
                       f"→ {new_total} disponible(s).")
        self._push_resources()
        return st

    async def full_sync(self) -> None:
        self._set_status("syncing")
        self._emit("sync", "Synchronisation en cours…")
        await self.sync_status()
        collection = await self.client.get_collection()

        # 1) Affichage IMMÉDIAT : indices owned/total depuis /api/collection.
        for entry in collection:
            sid = entry["series_id"]
            seed = SERIES_SEED.get(sid, {})
            self.db.upsert_series(sid, seed.get("name", _prettify(sid)),
                                  DEFAULT_PRIMARY, DEFAULT_ACCENT, seed.get("size", 0))
            self.db.set_collection_hint(sid, entry.get("owned"), entry.get("total_pulls"))
        self._push_progress()

        # 2) Détail par série (inventaire exact) — affine au fil de l'eau (lectures rapides).
        for entry in collection:
            sid = entry["series_id"]
            try:
                detail = await self.client.get_series_detail(sid)
                items = [{"card_id": d["card_id"], "rarity": d.get("rarity"),
                          "quantity": d.get("quantity", 1),
                          "first_pulled": d.get("first_pulled")} for d in (detail or [])]
                self.db.replace_inventory(sid, items)
                self._push_progress()
            except ApiError as exc:
                self._emit("sync", f"Détail {sid} ignoré : {exc}", series_id=sid, level="warn")

        # 3) Catalogue (cartes manquantes / marketplace) — chargé après, optionnel.
        if self.settings.engine.get("fetch_catalog", True):
            for entry in collection:
                sid = entry["series_id"]
                if self.db.catalog_size(sid) == 0:
                    try:
                        cat = await self.client.get_series_catalog(sid)
                        cards = cat.get("cards", []) if isinstance(cat, dict) else []
                        self.db.replace_catalog(sid, cards)
                        if not SERIES_SEED.get(sid, {}).get("size") and cards:
                            self.db.upsert_series(sid, _prettify(sid), DEFAULT_PRIMARY,
                                                  DEFAULT_ACCENT, len(cards))
                    except ApiError as exc:
                        self._emit("sync", f"Catalogue {sid} indisponible : {exc}",
                                   series_id=sid, level="warn")

        self._push_progress()
        self._last_full_sync = time.time()
        self._emit("sync", "Synchronisation terminée.")
        self._set_status("running" if self.running else "idle")

    # Actions (open_one, _cancel_obsolete_listings, _try_open_mystery, recycle_pass)
    #   → engine_actions.ActionsMixin
    # Marketplace (marketplace_pass, _fulfill_useful)
    #   → engine_market.MarketMixin

    # ------------------------------------------------------------------ #
    #  Achat de packs avec l'encre (restock)
    # ------------------------------------------------------------------ #
    async def _try_buy_packs(self) -> bool:
        """Achète un restock complet si l'encre le permet (en gardant une réserve).
        L'encre ne sert QU'À ça. Renvoie True si un achat a eu lieu."""
        cost = int(self.settings.packs.get("full_restock_ink", 400))
        reserve = int(self.settings.engine.get("min_ink_reserve", 0))
        ink = self.resources.get("ink", 0)
        if ink < cost + reserve:
            log.debug("Achat de packs ignoré : encre %d < coût %d + réserve %d", ink, cost, reserve)
            return False
        log.info("Achat d'un restock : encre %d ≥ coût %d (+ réserve %d)", ink, cost, reserve)
        try:
            res = await self.client.regen_packs("full")
        except ApiError as exc:
            self._emit("buy", f"Achat de packs refusé : {exc}", level="warn")
            return False
        if not (res.get("success") or res.get("freePacks") is not None):
            return False
        self.resources["free_packs"] = res.get("freePacks", self.resources.get("free_packs", 0))
        self.resources["total_available"] = res.get("totalAvailable", self.resources["free_packs"])
        if res.get("newBalance") is not None:
            self.resources["ink"] = res["newBalance"]
        self._emit("buy", f"Restock acheté ({cost} encre) → {self.resources['total_available']} packs",
                   ink_delta=-cost, detail={"new_balance": self.resources.get("ink")})
        self._push_resources()
        return True

    # ------------------------------------------------------------------ #
    #  Boucle principale — 3 tâches PARALLÈLES (ouverture / recyclage / marketplace)
    #
    #  Les trois tournent en concurrence et partagent le client : le throttle global
    #  SÉRIALISE déjà toutes les requêtes HTTP (anti‑429), donc la parallélisation
    #  n'augmente pas le débit réseau — elle DÉCOUPLE les cadences : la marketplace et le
    #  recyclage restent réactifs même quand l'ouverture attend la régénération, et une
    #  carte tirée annule aussitôt l'annonce correspondante. Arrêt coopératif via
    #  self.running ; une AuthError stoppe tout (réessayer ne sert à rien).
    # ------------------------------------------------------------------ #
    def _wake_opener(self) -> None:
        """Réveille la boucle d'ouverture si elle dort (ex. après un gain d'encre)."""
        self._wake.set()

    def _on_fatal_auth(self, exc: AuthError) -> None:
        self.auth_error = str(exc)
        self.running = False
        self._emit("error", f"Authentification : {exc} — colle un nouveau cookie de session.",
                   level="error")
        self._set_status("error")

    async def _run(self) -> None:
        try:
            await self.full_sync()
            self._set_status("running")
            # gather : si on annule la tâche superviseur (stop), les sous-tâches le sont aussi.
            await asyncio.gather(self._open_loop(), self._recycle_loop(),
                                 self._marketplace_loop(), self._status_loop())
        except AuthError as exc:
            self._on_fatal_auth(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # filet : on ne laisse jamais le superviseur crasher l'app
            log.exception("Erreur inattendue dans le superviseur")
            self._emit("error", f"Erreur inattendue : {exc}", level="error")
            self._set_status("error")
        finally:
            self.running = False
            if self.status != "error":
                self._set_status("stopped")

    @staticmethod
    def _fmt_dur(seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        m, r = divmod(s, 60)
        return f"{m}min" if r == 0 else f"{m}min{r:02d}s"

    def _idle_wait_seconds(self) -> float:
        """Combien attendre quand il n'y a plus rien à faire côté ouverture/achat.
        On vise le PROCHAIN pack gratuit connu (nextRegenAt) ; sinon backoff croissant plafonné."""
        base = float(self.settings.engine.get("idle_poll_seconds", 60.0))
        cap = float(self.settings.engine.get("idle_poll_max", 1800.0))
        regen_ms = self.resources.get("next_regen_at")
        if regen_ms:
            until = regen_ms / 1000.0 - time.time() + 3.0   # juste après la régén
            if until > base:
                return min(until, cap)
        # pas d'info de régén exploitable -> on augmente progressivement
        self._idle_backoff = min(self._idle_backoff * 2, cap) if self._idle_backoff else base
        return self._idle_backoff

    async def _open_loop(self) -> None:
        """Ouvre les packs présents selon la stratégie ; quand il n'y en a plus, achète à
        l'encre (en finançant par recyclage si besoin) ou attend la régénération."""
        while self.running:
            try:
                if self.resources.get("total_available", 0) <= 0:
                    await self.sync_status()
                available = self.resources.get("total_available", 0)
                if available > 0:
                    self._idle_backoff = 0.0   # progrès possible → on repart en cadence normale

                if available <= 0:
                    if self.settings.engine.get("buy_packs_with_ink"):
                        if await self._try_buy_packs():
                            continue
                        if self.settings.engine.get("auto_recycle", True):
                            cost = int(self.settings.packs.get("full_restock_ink", 400))
                            reserve = int(self.settings.engine.get("min_ink_reserve", 0))
                            if await self.recycle_pass(target_ink=cost + reserve) \
                                    and await self._try_buy_packs():
                                continue
                    if self.settings.engine.get("on_empty") == "stop":
                        self._emit("idle", "Plus de packs — arrêt (on_empty=stop).")
                        self.running = False
                        break
                    # Rien à faire ici (ni encre ni recyclage finançable) : on attend.
                    # On se cale sur le PROCHAIN pack gratuit connu (nextRegenAt) ; sinon
                    # backoff croissant plafonné (évite de recharger toutes les minutes).
                    # MAIS l'attente est INTERRUPTIBLE : si le recyclage gagne assez d'encre,
                    # il pose `_wake` et on se réveille aussitôt pour acheter (vraie coordination).
                    wait = self._idle_wait_seconds()
                    self._set_status("waiting")
                    self._emit("idle", f"Plus de packs — attente {self._fmt_dur(wait)} "
                               "(régén/marketplace en parallèle ; réveil si encre suffisante).")
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=wait)
                        self._idle_backoff = 0.0   # réveillé par un gain d'encre → on retente vite
                    except asyncio.TimeoutError:
                        pass
                    if self.running:
                        self._set_status("running")
                    continue

                # Pack mystery (premium, ~1×/6h) : prioritaire dès qu'il est dû et qu'un pack
                # est disponible — on lui consacre un pack avant les séries normales.
                if (self.settings.engine.get("mystery_pack", True)
                        and time.time() >= self._mystery_due_at):
                    await self._try_open_mystery()
                    continue   # le compte de packs a changé → on reboucle

                progress = self.db.progress_view()
                only = self.settings.engine.get("only_series")
                if only:
                    # Mode mono-série (orchestrateur multi-comptes) : on ouvre TOUJOURS cette
                    # série, même complète (les doublons deviennent matière d'échange pour les LR).
                    target = next((p for p in progress if p["series_id"] == only), None) or {
                        "series_id": only, "name": _prettify(only),
                        "missing": 0, "total": 0, "pct": 0.0}
                else:
                    target = strategy.select_target_series(progress)
                    if target is None:
                        self._emit("done", "Toutes les séries connues sont complètes 🎉")
                        self.running = False
                        break
                if target["series_id"] != self._last_target:
                    self._last_target = target["series_id"]
                    log.info("Cible : %s — %d manquante(s)/%d (%.1f%%) [série la plus rentable]",
                             target["name"], target["missing"], target["total"], target["pct"])

                # Priorité absolue : ouvrir les packs présents. La marketplace n'est qu'un
                # OUTIL COMPLÉMENTAIRE (tâche parallèle) — elle n'interrompt jamais l'ouverture.
                if self.settings.engine.get("auto_open", True):
                    try:
                        await self.open_one(target["series_id"])
                    except ApiError as exc:
                        self._emit("open", f"Ouverture refusée ({exc}) — resynchronisation.",
                                   series_id=target["series_id"], level="warn")
                        self.resources["total_available"] = 0
                        await self.sync_status()
                        continue

                # resync périodique du statut (encre / packs réels)
                self._opens_since_resync += 1
                if self._opens_since_resync >= self.settings.engine.get("resync_every", 5):
                    self._opens_since_resync = 0
                    await self.sync_status()
                    # re-sync COMPLET périodique (opt-in) : recale l'inventaire/les séries si
                    # tu joues aussi dans le navigateur. 0 = désactivé.
                    fr = float(self.settings.engine.get("full_resync_minutes", 0)) * 60
                    if fr and (time.time() - self._last_full_sync) >= fr:
                        await self.full_sync()
            except AuthError as exc:
                self._on_fatal_auth(exc)
                return
            except asyncio.CancelledError:
                raise
            except ApiError as exc:
                self._emit("open", f"Erreur (ouverture) : {exc}", level="warn")
                await asyncio.sleep(3.0)
            except Exception as exc:
                log.exception("Erreur dans _open_loop")
                self._emit("error", f"Erreur inattendue (ouverture) : {exc}", level="error")
                await asyncio.sleep(3.0)

    async def _recycle_loop(self) -> None:
        """Recyclage périodique du surplus (mode "surplus"). En "on_demand", ne fait rien :
        le recyclage n'a alors lieu que pour financer un restock (dans _open_loop)."""
        interval = float(self.settings.engine.get("recycle_interval", 30.0))
        while self.running:
            await asyncio.sleep(interval)
            if not self.running:
                break
            if not (self.settings.engine.get("auto_recycle", True)
                    and self.settings.engine.get("recycle_mode", "surplus") != "on_demand"):
                continue
            # PRIORITÉ À L'OUVERTURE : s'il reste des packs à ouvrir, on diffère le recyclage
            # (sinon ses requêtes une-par-une monopolisent le throttle et retardent l'ouverture).
            # Le recyclage tourne donc surtout quand les packs sont épuisés (et finance les achats).
            if self.resources.get("total_available", 0) > 0:
                continue
            try:
                log.debug("Tick recyclage (toutes les %.0fs)", interval)
                await self.recycle_pass()
            except AuthError as exc:
                self._on_fatal_auth(exc)
                return
            except asyncio.CancelledError:
                raise
            except ApiError as exc:
                self._emit("recycle", f"Erreur (recyclage) : {exc}", level="warn")
            except Exception:
                log.exception("Erreur dans _recycle_loop")

    async def _marketplace_loop(self) -> None:
        """Maintien périodique des annonces (création/réponse/annulation des obsolètes).
        Ne fait rien tant que [marketplace] enabled est faux (réglable en direct).

        Backoff croissant plafonné quand une passe ne fait RIEN (rien à créer ni à honorer) :
        on ne re-sonde pas le marché des autres toutes les N s pour rien. Cadence rapide
        rétablie dès qu'une action a lieu."""
        base = float(self.settings.engine.get("marketplace_interval", 45.0))
        cap = float(self.settings.engine.get("marketplace_interval_max", 900.0))

        def grow() -> float:   # espace la prochaine passe (backoff croissant plafonné)
            return min((self._mkt_backoff or base) * 2, cap)

        while self.running:
            await asyncio.sleep(self._mkt_backoff or base)
            if not self.running:
                break
            if not self.settings.marketplace.get("enabled"):
                self._mkt_backoff = 0.0
                continue
            # Priorité à l'ouverture MAIS cadence marketplace GARANTIE : tant qu'il reste des packs
            # à ouvrir, on diffère la marketplace — sauf si la dernière passe remonte à plus de
            # `marketplace_min_interval` (sinon, avec l'encre quasi infinie, l'ouverture ne s'arrête
            # jamais et la marketplace n'est jamais servie). On lui garantit donc ≥ 1 passe / N s.
            min_interval = float(self.settings.engine.get("marketplace_min_interval", 120.0))
            if (self.resources.get("total_available", 0) > 0
                    and (time.time() - self._last_mkt_pass) < min_interval):
                continue
            try:
                log.debug("Tick marketplace (backoff=%.0fs)", self._mkt_backoff or base)
                self._last_mkt_pass = time.time()
                acted = await self.marketplace_pass()
            except AuthError as exc:
                self._on_fatal_auth(exc)
                return
            except asyncio.CancelledError:
                raise
            except ApiError as exc:
                self._emit("listing", f"Erreur (marketplace) : {exc}", level="warn")
                self._mkt_backoff = grow()
                continue
            except Exception:
                log.exception("Erreur dans _marketplace_loop")
                continue
            # action -> cadence rapide ; rien -> on espace (jusqu'au plafond)
            self._mkt_backoff = 0.0 if acted else grow()

    async def _status_loop(self) -> None:
        """Re-sync LÉGER et fréquent du statut (encre / packs réels / régén) — pour toujours
        connaître les vrais chiffres et détecter une régénération sans dépendre des autres
        boucles. GET peu coûteux ; cadence `status_sync_seconds`."""
        interval = float(self.settings.engine.get("status_sync_seconds", 15.0))
        while self.running:
            await asyncio.sleep(interval)
            if not self.running:
                break
            try:
                await self.sync_status()
                # Régén détectée pendant l'attente -> on réveille l'ouverture pour ouvrir aussitôt.
                if self.resources.get("total_available", 0) > 0:
                    self._wake_opener()
            except AuthError as exc:
                self._on_fatal_auth(exc)
                return
            except asyncio.CancelledError:
                raise
            except ApiError:
                pass
            except Exception:
                log.exception("Erreur dans _status_loop")

    # ------------------------------------------------------------------ #
    #  Contrôle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._recycle_warned = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._set_status("stopped")
        self._emit("control", "Arrêt demandé.")

    async def sync_now(self) -> None:
        await self.full_sync()
