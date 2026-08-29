"""Test utilities: FakeClient (mimics WikiTCGClient with no network) + engine factory.
The response shapes reproduce the real HAR captures from the live API."""
from __future__ import annotations

import tempfile, os, uuid

from app.api_client import ApiError
from app.config import Settings, _DEFAULTS, _deep_merge
from app.db import Database
from app.events import EventBus
from app.engine import Engine


DEFAULT_STATUS = {
    "ink": 1000, "freePacks": 5, "paidPacks": 0, "totalAvailable": 5, "maxFreePacks": 5,
    "nextRegenAt": None, "level": 30, "xp": 100, "xpToNext": 50, "xpProgress": 0.5,
    "streak": 7,
}


class FakeClient:
    """Mimics the interface `Engine` uses. No network. Configurable."""

    def __init__(self, *, status=None, dups=None, locked=None, open_result=None,
                 mine=None, browse=None, quota_after=None, notfound=None):
        self.status = dict(DEFAULT_STATUS, **(status or {}))
        self.ink = self.status["ink"]
        self._dups = dups or []                       # normalized get_duplicates shape
        self.locked = set(locked or [])               # pullIds that return 500 on recycling
        self.quota_after = quota_after                # after N OK recycles, /recycle returns 429 (daily quota)
        self.notfound = set(notfound or [])           # pullIds that return 400 cards_not_found
        self._open_result = open_result
        self._mine = mine or []
        self._browse = browse or []
        # call logs (assertions)
        self.recycled, self.created, self.fulfilled, self.cancelled, self.opened = [], [], [], [], []
        self.session_cookie = "eyJ.fake.sig"

    async def get_status(self):
        return dict(self.status, ink=self.ink, totalAvailable=self.status["totalAvailable"])

    async def get_collection(self):
        return []

    async def get_series_detail(self, sid):
        return []

    async def get_series_catalog(self, sid):
        return {"cards": []}

    async def get_duplicates(self, recycle_cfg):
        return [dict(d) for d in self._dups]

    async def open_pack(self, series_id):
        self.opened.append(series_id)
        if self._open_result is not None:
            return self._open_result
        return {"cards": [{"id": "wiki-new1", "rarity": "C", "article": {"title": "X"}}],
                "series": {"name": series_id.title()}, "xpEarned": 10,
                "totalAvailable": max(self.status["totalAvailable"] - 1, 0)}

    async def recycle(self, pull_ids, *, retry_5xx=True, retry_429=True):
        pid = pull_ids[0]
        if pid in self.locked:
            raise ApiError("Server error 500", status=500)
        if pid in self.notfound:
            raise ApiError("API error 400", status=400, body='{"error":"cards_not_found"}')
        if self.quota_after is not None and len(self.recycled) >= self.quota_after:
            raise ApiError("Recycle quota/limit reached (429)", status=429)
        self.recycled.append(pid)
        self.ink += 40
        return {"recycled": 1, "inkEarned": 40, "newBalance": self.ink}

    async def regen_packs(self, type_="full"):
        self.ink -= 400
        self.status["totalAvailable"] = 5
        return {"success": True, "newBalance": self.ink, "freePacks": 5, "totalAvailable": 5}

    async def marketplace_mine(self):
        return {"listings": list(self._mine)}

    async def marketplace_browse(self, params=None):
        return {"listings": list(self._browse)}

    async def marketplace_create(self, offered_type, offered_series, wanted_card, wanted_series):
        self.created.append((offered_type, wanted_card))
        return {"success": True}

    async def marketplace_fulfill(self, listing_id):
        self.fulfilled.append(listing_id)
        return {"success": True}

    async def marketplace_cancel(self, listing_id):
        self.cancelled.append(listing_id)
        return {"success": True}

    async def aclose(self):
        pass


def make_settings(**engine_over) -> Settings:
    over = {"engine": engine_over} if engine_over else {}
    return Settings(raw=_deep_merge(_DEFAULTS, over))


def make_engine(client=None, *, settings=None, **engine_over):
    """Return (engine, client, db, path) ready for async tests."""
    client = client or FakeClient()
    settings = settings or make_settings(**engine_over)
    path = os.path.join(tempfile.gettempdir(), f"_wtcg_{uuid.uuid4().hex}.db")
    db = Database(path)
    eng = Engine(client, db, EventBus(), settings)
    return eng, client, db, path
