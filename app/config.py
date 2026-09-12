"""Built-in settings; changes made in the dashboard are stored in the app database."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .auth import is_real_session


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
        "auto_recycle": True, "fetch_catalog": True, "resync_every": 5,
        "buy_packs_with_ink": False, "min_ink_reserve": 0,
        "recycle_mode": "surplus",
        "recycle_reserve_mode": "fixed",
        "recycle_priority": "common_first",
        "only_series": "",
        "only_series_mode": "fallback",
        "series_check_minutes": 30.0,
        "mystery_pack": True,
        "mystery_interval_hours": 6.0,
        "autostart": False,
        "status_sync_seconds": 15.0,
        "recycle_interval": 30.0,
        "recycle_retry_minutes": 30.0,
        "recycle_quota_cooldown_minutes": 60.0,
        "full_resync_minutes": 0.0,
        "marketplace_interval": 45.0,
        "marketplace_interval_max": 900.0,
        "marketplace_min_interval": 120.0,
        "idle_poll_seconds": 60.0, "idle_poll_max": 1800.0,
        "keep_spares": {"C": 0, "UC": 1, "R": 1, "SR": 1, "SSR": 1, "UR": 2, "LR": 2},
        "recycle_skip_rarities": [],
    },
    "packs": {"full_restock_ink": 400, "max_free_packs": 5},
    "marketplace": {
        "enabled": False, "max_listings": 5, "fulfill_others": False,
        "fulfill_per_pass": 3, "prefer_near_completion": True,
        "max_listing_age_hours": 24.0,
        "near_completion_max_missing": 0,
    },
    "recycle": {
        "duplicates_endpoint": "/api/cards/duplicates", "max_per_call": 20,
        "list_key": "", "id_field": "", "type_field": "", "quantity_field": "",
        "values": {"LR": 500, "UR": 250, "SSR": 100, "SR": 40, "R": 0, "UC": 0, "C": 0},
    },
    "server": {"host": "127.0.0.1", "port": 8765},
    "paths": {"database": "app.db", "log_file": "wikitcg.log"},
    "logging": {"level": "INFO", "log_requests": False},
}


def _deep_merge(base: dict, over: dict) -> dict:
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


def load_settings() -> Settings:
    return Settings(raw=_deep_merge(_DEFAULTS, {}))
