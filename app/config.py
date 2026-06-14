"""Chargement de la configuration TOML avec valeurs par défaut robustes."""
from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


# ---- valeurs par défaut (utilisées si une clé manque dans le TOML) ----
_DEFAULTS: dict = {
    "api": {
        "base_url": "https://wikitcg.net",
        "session_cookie": "",
        "extra_cookies": "",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) "
                      "Gecko/20100101 Firefox/151.0",
        "http2": True,
    },
    "throttle": {
        "read_interval": 0.4, "read_jitter": 0.6,
        "min_interval": 2.0, "jitter": 2.5,
        "cooldown_every": 8, "cooldown_seconds": 25.0, "cooldown_jitter": 15.0,
    },
    "retry": {
        "max_retries": 5, "backoff_base": 2.0,
        "backoff_cap": 120.0, "backoff_jitter": 0.5,
    },
    "engine": {
        "auto_open": True, "auto_recycle": True, "fetch_catalog": True, "resync_every": 5,
        "buy_packs_with_ink": False, "min_ink_reserve": 0,
        "recycle_mode": "surplus",     # "surplus" (vider le surplus) | "on_demand" (financer les restocks)
        "recycle_reserve_mode": "fixed",  # "fixed" (keep_spares) | "missing" (garder autant que de manquantes/rareté)
        "recycle_priority": "common_first",  # ordre de recyclage : "common_first" (préserve les rares) | "rare_first" (max d'encre — farm pur)
        "only_series": "",             # "" = stratégie multi-séries ; sinon n'ouvre QUE cette série (orchestrateur multi-comptes)
        "mystery_pack": True,          # ouvrir le pack mystery (premium) dès qu'il est dispo (~1×/6h)
        "mystery_interval_hours": 6.0, # intervalle entre deux ouvertures du pack mystery
        "autostart": False,            # relancer la boucle automatiquement au démarrage du serveur
        "status_sync_seconds": 15.0,   # re-sync léger encre/packs (tâche dédiée) — connaître les vrais chiffres
        "recycle_interval": 30.0,      # cadence (s) de la tâche de recyclage parallèle
        "recycle_retry_minutes": 30.0, # délai de base avant de re-tenter une carte 500 (croît selon les échecs)
        "recycle_quota_cooldown_minutes": 60.0,  # pause du recyclage après un 429 (quota journalier ~200/j)
        "full_resync_minutes": 0.0,    # 0 = off ; >0 = re-sync complet périodique (exactitude si jeu en parallèle)
        "marketplace_interval": 45.0,  # cadence (s) de la tâche marketplace parallèle
        "marketplace_interval_max": 900.0,  # plafond du backoff marketplace (rien à faire)
        "marketplace_min_interval": 120.0,  # cadence GARANTIE (s) même si des packs restent à ouvrir
        "on_empty": "wait", "idle_poll_seconds": 60.0, "idle_poll_max": 1800.0,
        "keep_spares": {"C": 0, "UC": 1, "R": 1, "SR": 1, "SSR": 1, "UR": 2, "LR": 2},
        "recycle_skip_rarities": [],   # raretés JAMAIS recyclées (ex. ["LR"]) — gardées intégralement
    },
    "packs": {"full_restock_ink": 400, "max_free_packs": 5},
    "marketplace": {
        "enabled": False, "max_listings": 5, "fulfill_others": False,
        "fulfill_per_pass": 3, "prefer_near_completion": True,
        "dry_run": False,   # true = journalise ce qui SERAIT fait, sans aucune écriture (validation)
        "max_listing_age_hours": 24.0,   # annonce plus vieille que ça -> annulée puis recréée (0 = jamais)
        "near_completion_max_missing": 0,  # ne cibler QUE les séries à qui il manque ≤ N cartes (0 = toutes)
    },
    "recycle": {
        "duplicates_endpoint": "/api/cards/duplicates", "max_per_call": 20,
        "list_key": "", "id_field": "", "type_field": "", "quantity_field": "",
        "values": {"LR": 500, "UR": 250, "SSR": 100, "SR": 40, "R": 0, "UC": 0, "C": 0},
    },
    "server": {"host": "127.0.0.1", "port": 8765},
    "paths": {"database": "wikitcg.db", "log_file": "wikitcg.log"},
    "logging": {"level": "INFO", "log_requests": False},
}


def _deep_merge(base: dict, over: dict) -> dict:
    # Deep-copy : le résultat ne PARTAGE aucune structure mutable avec `base` (ex. _DEFAULTS) ni
    # `over`. Indispensable : sans ça, modifier settings.raw[section][clé] muterait les défauts
    # globaux (fuite entre load_settings / reset_config / instances).
    out = {k: copy.deepcopy(v) for k, v in base.items()}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class Settings:
    raw: dict = field(default_factory=dict)

    # --- accès groupés ---
    @property
    def api(self) -> dict: return self.raw["api"]
    @property
    def throttle(self) -> dict: return self.raw["throttle"]
    @property
    def retry(self) -> dict: return self.raw["retry"]
    @property
    def engine(self) -> dict: return self.raw["engine"]
    @property
    def recycle(self) -> dict: return self.raw["recycle"]
    @property
    def packs(self) -> dict: return self.raw["packs"]
    @property
    def marketplace(self) -> dict: return self.raw["marketplace"]
    @property
    def server(self) -> dict: return self.raw["server"]
    @property
    def paths(self) -> dict: return self.raw["paths"]
    @property
    def logging(self) -> dict: return self.raw["logging"]

    @property
    def has_session(self) -> bool:
        c = self.api.get("session_cookie", "")
        return bool(c) and "PASTE_YOUR" not in c


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    """Charge config.toml (ou $WIKITCG_CONFIG), fusionné avec les défauts."""
    candidate = (
        path
        or os.environ.get("WIKITCG_CONFIG")
        or ("config.toml" if Path("config.toml").exists() else None)
    )
    user_cfg: dict = {}
    if candidate and Path(candidate).exists():
        with open(candidate, "rb") as fh:
            user_cfg = tomllib.load(fh)
    settings = Settings(raw=_deep_merge(_DEFAULTS, user_cfg))
    # Surcharges par variables d'environnement (prioritaires — évite de stocker le secret en clair).
    if os.environ.get("WIKITCG_SESSION"):
        settings.raw["api"]["session_cookie"] = os.environ["WIKITCG_SESSION"]
    if os.environ.get("WIKITCG_EXTRA_COOKIES"):
        settings.raw["api"]["extra_cookies"] = os.environ["WIKITCG_EXTRA_COOKIES"]
    return settings
