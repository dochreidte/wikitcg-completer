"""TOML config loading with robust defaults."""
from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .auth import is_real_session


# ---- defaults (used when a key is missing from the TOML) ----
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
        "recycle_mode": "surplus",     # "surplus" (drain the surplus) | "on_demand" (fund restocks)
        "recycle_reserve_mode": "fixed",  # "fixed" (keep_spares) | "missing" (keep as many as missing/rarity)
        "recycle_priority": "common_first",  # recycle order: "common_first" (preserve rares) | "rare_first" (max ink — pure farm)
        "only_series": "",             # "" = multi-series strategy; otherwise open ONLY this series (multi-account orchestrator)
        "mystery_pack": True,          # open the mystery (premium) pack as soon as it's available (~1x/6h)
        "mystery_interval_hours": 6.0, # interval between two mystery-pack openings
        "autostart": False,            # restart the loop automatically when the server starts
        "status_sync_seconds": 15.0,   # light ink/packs re-sync (dedicated task) — know the real numbers
        "recycle_interval": 30.0,      # cadence (s) of the parallel recycle task
        "recycle_retry_minutes": 30.0, # base delay before retrying a 500 card (grows with failures)
        "recycle_quota_cooldown_minutes": 60.0,  # recycle pause after a 429 (daily quota ~200/day)
        "full_resync_minutes": 0.0,    # 0 = off; >0 = periodic full re-sync (accuracy if playing in parallel)
        "marketplace_interval": 45.0,  # cadence (s) of the parallel marketplace task
        "marketplace_interval_max": 900.0,  # marketplace backoff cap (nothing to do)
        "marketplace_min_interval": 120.0,  # GUARANTEED cadence (s) even if packs remain to open
        "on_empty": "wait", "idle_poll_seconds": 60.0, "idle_poll_max": 1800.0,
        "keep_spares": {"C": 0, "UC": 1, "R": 1, "SR": 1, "SSR": 1, "UR": 2, "LR": 2},
        "recycle_skip_rarities": [],   # rarities NEVER recycled (e.g. ["LR"]) — kept in full
    },
    "packs": {"full_restock_ink": 400, "max_free_packs": 5},
    "marketplace": {
        "enabled": False, "max_listings": 5, "fulfill_others": False,
        "fulfill_per_pass": 3, "prefer_near_completion": True,
        "dry_run": False,   # true = log what WOULD be done, with no writes at all (validation)
        "max_listing_age_hours": 24.0,   # listing older than this -> cancelled then recreated (0 = never)
        "near_completion_max_missing": 0,  # target ONLY series missing <= N cards (0 = all)
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
    # Deep-copy: the result SHARES no mutable structure with `base` (e.g. _DEFAULTS) or
    # `over`. Essential: without it, mutating settings.raw[section][key] would mutate the
    # global defaults (leakage between load_settings / reset_config / instances).
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

    # --- grouped access ---
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
        return is_real_session(self.api.get("session_cookie", ""))


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    """Load config.toml (or $WIKITCG_CONFIG), merged with the defaults."""
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
    # Environment-variable overrides (highest priority — avoids storing the secret in clear text).
    if os.environ.get("WIKITCG_SESSION"):
        settings.raw["api"]["session_cookie"] = os.environ["WIKITCG_SESSION"]
    if os.environ.get("WIKITCG_EXTRA_COOKIES"):
        settings.raw["api"]["extra_cookies"] = os.environ["WIKITCG_EXTRA_COOKIES"]
    return settings
