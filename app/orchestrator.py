"""Orchestrateur multi-comptes : farm séquentiel d'UNE série par compte.

But : pour CHAQUE compte, ouvrir des packs de sa série (`engine.only_series`) et recycler
les doublons pour racheter des packs à l'encre, EN BOUCLE, jusqu'à ce qu'on ne puisse plus
ouvrir de pack à l'encre — puis passer au compte suivant. Les échanges (LR) sont gérés à la
main, hors de ce programme.

Modèle d'exécution : SÉQUENTIEL — un seul compte actif à la fois. Le moteur boucle
open → (plus de packs) → recycle le MINIMUM (rare-first) pour financer → rachat d'un restock
→ open … On recycle au plus juste, en sacrifiant d'abord les cartes les plus RARES (encre
maximale par recyclage, quota journalier préservé) ; le surplus n'est PAS vidé. Quand il ne
peut PLUS ouvrir de pack à l'encre (plus de packs gratuits ET encre insuffisante même après
recyclage), il passe en statut `waiting` : c'est le signal de BASCULE vers le compte suivant.
On boucle indéfiniment sur la liste ; quand un tour complet ne produit AUCUNE ouverture (tous
les comptes épuisés), on patiente `idle_cycle_minutes` avant de recommencer (le temps que les
packs gratuits / le quota de recyclage se régénèrent).

Pas de marketplace ici (open + recycle seulement) : les échanges restent manuels.

Lancement : `python run_multi.py`  (lit accounts.toml à la racine du projet).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .api_client import WikiTCGClient
from .config import Settings, _deep_merge, load_settings
from .db import Database
from .engine import Engine
from .events import EventBus

log = logging.getLogger("wikitcg.orchestrator")


@dataclass
class Account:
    name: str
    series: str
    session_cookie: str
    extra_cookies: str = ""        # ex. "cf_clearance=..."
    mystery: bool = False          # ouvrir aussi le pack mystery (autre pool) — défaut non
    db_path: str = ""              # base SQLite dédiée (inventaire propre au compte)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "compte"


def load_accounts(path: str = "accounts.toml") -> tuple[list[Account], dict]:
    """Lit accounts.toml -> (liste de comptes valides, options [runner]).

    Forme attendue :
        [runner]
        poll_seconds = 8.0
        idle_cycle_minutes = 30.0

        [[account]]
        name = "..."; series = "..."; session_cookie = "..."; extra_cookies = "..."
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"{path} introuvable. Copie accounts.example.toml en accounts.toml et renseigne tes comptes.")
    with open(p, "rb") as fh:
        data = tomllib.load(fh)

    runner = dict(data.get("runner", {}) or {})
    accounts: list[Account] = []
    seen: dict[str, int] = {}
    for i, raw in enumerate(data.get("account", []) or [], start=1):
        name = str(raw.get("name") or f"compte{i}").strip()
        series = str(raw.get("series") or "").strip()
        session = str(raw.get("session_cookie") or "").strip()
        if not series or not session or "PASTE_YOUR" in session:
            log.warning("Compte « %s » ignoré : series et session_cookie sont obligatoires.", name)
            continue
        # Noms en double (copier-coller fréquent) : on NE jette PAS — on désambiguïse pour que
        # chaque compte ait une base distincte (wikitcg_<slug>.db / <slug>-2.db…) et tourne quand même.
        slug = _slug(name)
        seen[slug] = seen.get(slug, 0) + 1
        uniq = slug if seen[slug] == 1 else f"{slug}-{seen[slug]}"
        label = name if seen[slug] == 1 else f"{name}#{seen[slug]}"
        accounts.append(Account(
            name=label, series=series, session_cookie=session,
            extra_cookies=str(raw.get("extra_cookies") or "").strip(),
            mystery=bool(raw.get("mystery", False)),
            db_path=f"wikitcg_{uniq}.db",
        ))
    return accounts, runner


class MultiAccountRunner:
    """Fait tourner les comptes l'un après l'autre (cf. en-tête de module)."""

    def __init__(self, accounts: list[Account], base_settings: Settings, *,
                 poll_seconds: float = 8.0, idle_cycle_minutes: float = 30.0):
        self.accounts = accounts
        self.base = base_settings
        self.poll = max(float(poll_seconds), 1.0)
        self.idle_cycle = max(float(idle_cycle_minutes), 0.0) * 60
        self.running = True
        # Bascule MANUELLE (depuis la page de suivi) : _skip = on quitte le compte actif au
        # prochain tick ; _goto = nom du compte à activer ensuite (None -> simplement le suivant).
        self._skip = False
        self._goto: str | None = None
        # État live pour la page de suivi (read-only).
        self.active: str | None = None      # compte actuellement actif
        self.cycle = 0                       # n° de tour sur la liste
        self.started_at = time.time()
        self.stats: dict[str, dict] = {
            a.name: {"series": a.series, "status": "en attente", "active": False,
                     "opened": 0, "recycled": 0, "ink": None, "packs": None, "level": None}
            for a in accounts
        }

    # ------------------------------------------------------------------ #
    async def _sleep_poll(self) -> None:
        """Dort jusqu'à `self.poll` s, mais se réveille presque tout de suite si une bascule
        manuelle est demandée (pour que le bouton de la page de suivi réagisse vite)."""
        step = 0.5
        waited = 0.0
        while waited < self.poll and self.running and not self._skip:
            await asyncio.sleep(min(step, self.poll - waited))
            waited += step

    def _settings_for(self, acc: Account) -> Settings:
        """Settings du compte : base globale + surcharges (cookies, série, garde-fous farm)."""
        over = {
            "api": {"session_cookie": acc.session_cookie, "extra_cookies": acc.extra_cookies},
            "engine": {
                "only_series": acc.series,        # n'ouvre QUE cette série
                "mystery_pack": acc.mystery,
                "buy_packs_with_ink": True,        # le farm rachète des packs avec l'encre recyclée
                "auto_open": True, "auto_recycle": True,
                # Boucle de farm la plus SERRÉE possible : on ouvre les packs, puis on recycle le
                # STRICT MINIMUM (et rien de plus) pour financer le prochain restock, en sacrifiant
                # d'abord les cartes les plus RARES (rare-first) → encre MAXIMALE par recyclage et
                # quota journalier (~200/j) préservé. On rouvre le restock et on recommence :
                # ouvrir → recycler le minimum → racheter → ouvrir.
                # NB : le site ne vend QUE le restock complet (5 packs / 400 encre) — l'achat d'un
                # pack à l'unité n'existe pas, donc l'unité de rachat de la boucle est 1 restock.
                "recycle_mode": "on_demand",       # pas de vidage du surplus : recycle au besoin
                "recycle_priority": "rare_first",  # recycle les + rares d'abord → max d'encre/carte
                "on_empty": "wait",                # -> statut `waiting` = signal de bascule
                "autostart": False,
            },
            "marketplace": {"enabled": False},     # open + recycle seulement
            "paths": {"database": acc.db_path, "log_file": self.base.paths["log_file"]},
        }
        return Settings(raw=_deep_merge(self.base.raw, over))

    async def _run_account(self, acc: Account) -> bool:
        """Fait tourner un compte jusqu'à épuisement (statut `waiting`) ou erreur.
        Renvoie True si le compte a ouvert au moins un pack pendant la visite."""
        settings = self._settings_for(acc)
        client = WikiTCGClient(settings)
        client.session_sink = lambda tok, n=acc.name: log.info(
            "Cookie de session rafraîchi pour « %s » — pense à l'actualiser dans accounts.toml.", n)
        db = Database(acc.db_path)
        engine = Engine(client, db, EventBus(), settings)
        st = self.stats[acc.name]
        st.update(active=True, status="démarrage")
        self.active = acc.name
        log.info("════ Compte « %s » → série %s (db=%s) ════", acc.name, acc.series, acc.db_path)
        engine.start()

        def _refresh() -> None:   # recopie l'état du moteur dans les stats de suivi
            r = engine.resources or {}
            st.update(status=engine.status, opened=engine.opened_total,
                      recycled=engine.recycled_total, ink=r.get("ink"),
                      packs=r.get("total_available"), level=r.get("level"))

        try:
            # On laisse le moteur démarrer (sync) puis on surveille son statut. On bascule dès
            # qu'il se met en attente (plus rien à faire) ou s'arrête (terminé / erreur cookie).
            while self.running and engine.running:
                await self._sleep_poll()
                _refresh()
                if self._skip:
                    log.info("Compte « %s » : bascule manuelle demandée — %d ouvert(s). %s",
                             acc.name, engine.opened_total,
                             f"Activation de « {self._goto} »." if self._goto
                             else "Passage au compte suivant.")
                    break
                if engine.status == "waiting":
                    log.info("Compte « %s » : plus de pack ouvrable à l'encre (packs gratuits "
                             "épuisés + encre insuffisante même après recyclage) — %d ouvert(s). "
                             "Passage au compte suivant.", acc.name, engine.opened_total)
                    break
                if engine.status == "error":
                    log.warning("Compte « %s » en erreur (%s) — on passe au suivant.",
                                acc.name, engine.auth_error or "voir logs")
                    break
        finally:
            _refresh()
            st["active"] = False
            st["status"] = ("erreur" if engine.status == "error"
                            else "épuisé" if engine.status == "waiting" else "arrêté")
            if self.active == acc.name:
                self.active = None
            await engine.stop()
            await client.aclose()
            db.close()
        return engine.opened_total > 0

    async def run(self) -> None:
        if not self.accounts:
            log.error("Aucun compte valide dans accounts.toml — rien à faire.")
            return
        log.info("Orchestrateur multi-comptes : %d compte(s) — %s",
                 len(self.accounts), ", ".join(f"{a.name}:{a.series}" for a in self.accounts))
        while self.running:
            self.cycle += 1
            worked_any = False
            idx = 0
            while idx < len(self.accounts) and self.running:
                # Bascule manuelle : si un compte a été explicitement choisi, on saute dessus.
                if self._goto is not None:
                    target, self._goto = self._goto, None
                    j = next((k for k, a in enumerate(self.accounts) if a.name == target), None)
                    if j is not None:
                        idx = j
                acc = self.accounts[idx]
                try:
                    worked_any = await self._run_account(acc) or worked_any
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Erreur inattendue sur le compte « %s » — on continue.", acc.name)
                self._skip = False  # demande de bascule consommée
                # Si une cible a été posée pendant la visite, on la traite en tête de boucle
                # (sans avancer) ; sinon on passe au compte suivant de la liste.
                if self._goto is None:
                    idx += 1
            if not self.running:
                break
            # Tour complet sans aucune ouverture -> tous les comptes sont épuisés : on patiente
            # (régénération des packs gratuits / réinitialisation du quota de recyclage).
            if not worked_any and self.idle_cycle > 0:
                mins = int(self.idle_cycle / 60)
                log.info("Tous les comptes sont épuisés — pause %d min avant un nouveau tour.", mins)
                # Pause interruptible : une bascule manuelle (page de suivi) la coupe net.
                waited = 0.0
                while waited < self.idle_cycle and self.running and not self._skip:
                    await asyncio.sleep(min(2.0, self.idle_cycle - waited))
                    waited += 2.0
                # `_skip` a servi à couper la pause (aucun compte actif à quitter) : on le
                # consomme ici pour ne pas sauter d'emblée le 1er compte du tour suivant.
                # `_goto` est conservé pour que le saut vers le compte choisi ait bien lieu.
                self._skip = False

    def request_switch(self, account: str | None = None) -> dict:
        """Demande une bascule MANUELLE (appelée par la page de suivi).

        - `account` fourni  -> on quitte le compte actif puis on active CE compte.
        - `account` None     -> on quitte le compte actif et on passe simplement au suivant.
        Prise en compte au prochain tick de surveillance (≤ ~0,5 s)."""
        if account is not None:
            account = account.strip()
            known = {a.name for a in self.accounts}
            if account and account not in known:
                return {"ok": False, "error": f"compte inconnu : {account}"}
            self._goto = account or None
        self._skip = True
        return {"ok": True, "goto": self._goto, "from": self.active}

    def stop(self) -> None:
        self.running = False

    def status_snapshot(self) -> dict:
        """État courant pour la page de suivi (sérialisable JSON)."""
        return {
            "active": self.active,
            "cycle": self.cycle,
            "uptime_s": int(time.time() - self.started_at),
            "idle_cycle_min": int(self.idle_cycle / 60),
            "pending_switch": self._goto if self._skip else None,
            "accounts": [dict(name=a.name, **self.stats.get(a.name, {})) for a in self.accounts],
        }


async def run_from_config(accounts_path: str = "accounts.toml") -> None:
    """Charge la config globale + accounts.toml et lance l'orchestrateur (+ page de suivi)."""
    base = load_settings()
    accounts, runner = load_accounts(accounts_path)
    orch = MultiAccountRunner(
        accounts, base,
        poll_seconds=float(runner.get("poll_seconds", 8.0)),
        idle_cycle_minutes=float(runner.get("idle_cycle_minutes", 30.0)),
    )
    if not bool(runner.get("web", True)):
        await orch.run()
        return
    # Page de suivi (read-only) en parallèle, sur un port distinct du serveur mono-compte (8765).
    import uvicorn
    from .orchestrator_web import make_app
    host = str(runner.get("web_host", "127.0.0.1"))
    port = int(runner.get("web_port", 8766))
    server = uvicorn.Server(uvicorn.Config(make_app(orch), host=host, port=port, log_level="warning"))
    log.info("Page de suivi multi-comptes : http://%s:%d", host, port)
    web_task = asyncio.create_task(server.serve())
    try:
        await orch.run()
    finally:
        server.should_exit = True
        try:
            await web_task
        except asyncio.CancelledError:
            pass
