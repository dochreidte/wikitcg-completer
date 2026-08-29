"""Async API client for wikitcg.net.

Designed to be robust and extensible:
  * a global throttle (minimum interval + jitter) between ALL requests;
  * exponential backoff + jitter on 429 / 5xx / network errors;
  * a clear error taxonomy to decide retry behavior;
  * systematic logging.

Adding a new endpoint = add a small method that calls self._request().
Adding a new error rule = route it inside _request().
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

log = logging.getLogger("wikitcg.api")


def _extract_list(data, list_key: str = "") -> list:
    """Find the array of items in a response, via an explicit key or autodetection."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if list_key and isinstance(data.get(list_key), list):
            return data[list_key]
        for k in ("duplicates", "cards", "items", "data", "results"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def _pick(item: dict, explicit: str, candidates: list[str]):
    """Return the first value found: the explicit field (config), otherwise the default candidates."""
    if explicit and explicit in item:
        return item[explicit]
    for c in candidates:
        if c in item and item[c] not in (None, ""):
            return item[c]
    return None


def _parse_cookies(raw: str) -> dict:
    """Parse a Cookie header string ('k=v; k2=v2') into a dict. Empty -> {}."""
    out: dict = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            if k and k != "wtcg_session":   # the JWT is handled separately
                out[k] = v.strip()
    return out


# --------------------------------------------------------------------------- #
#  Error taxonomy
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    """Non-recoverable application error (4xx other than auth/429)."""
    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class AuthError(ApiError):
    """401/403 — expired/unauthorized cookie. We stop; a retry won't fix it."""


class RateLimited(Exception):
    """429 — internal, triggers the backoff."""
    def __init__(self, retry_after: float | None = None):
        super().__init__("429 Too Many Requests")
        self.retry_after = retry_after


# --------------------------------------------------------------------------- #
#  Global throttle
# --------------------------------------------------------------------------- #
class Throttle:
    """Spacing between requests. Two regimes:
      * reads (GET): short delay (sync stays fast);
      * actions (POST): longer delay + periodic pause (anti-429 on bursts of openings).
    """

    def __init__(self, min_interval: float, jitter: float,
                 cooldown_every: int, cooldown_seconds: float, cooldown_jitter: float,
                 read_interval: float = 0.4, read_jitter: float = 0.6):
        self.min_interval = min_interval
        self.jitter = jitter
        self.read_interval = read_interval
        self.read_jitter = read_jitter
        self.cooldown_every = max(cooldown_every, 0)
        self.cooldown_seconds = cooldown_seconds
        self.cooldown_jitter = cooldown_jitter
        self._last = 0.0
        self._counter = 0
        self._lock = asyncio.Lock()

    async def wait(self, *, mode: str = "action", heavy: bool = False) -> None:
        """`mode="read"` for GET requests (fast), `"action"` for POST.
        `heavy=True` counts the action toward the cooldown (e.g. opening a pack)."""
        async with self._lock:
            if mode == "read":
                gap = self.read_interval + random.uniform(0, self.read_jitter)
            else:
                gap = self.min_interval + random.uniform(0, self.jitter)
            elapsed = time.monotonic() - self._last
            if elapsed < gap:
                await asyncio.sleep(gap - elapsed)
            if heavy and self.cooldown_every:
                self._counter += 1
                if self._counter % self.cooldown_every == 0:
                    pause = self.cooldown_seconds + random.uniform(0, self.cooldown_jitter)
                    log.info("Anti-429 cooldown: pausing for %.0f s", pause)
                    await asyncio.sleep(pause)
            self._last = time.monotonic()


# --------------------------------------------------------------------------- #
#  Client
# --------------------------------------------------------------------------- #
class WikiTCGClient:
    def __init__(self, settings):
        api = settings.api
        self.base_url = api["base_url"].rstrip("/")
        self.retry_cfg = settings.retry
        t = settings.throttle
        self.throttle = Throttle(t["min_interval"], t["jitter"],
                                 t["cooldown_every"], t["cooldown_seconds"], t["cooldown_jitter"],
                                 t.get("read_interval", 0.4), t.get("read_jitter", 0.6))
        headers = {
            "User-Agent": api["user_agent"],
            "Accept": "*/*",
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
        }
        cookies = {"wtcg_session": api.get("session_cookie", "")}
        cookies.update(_parse_cookies(api.get("extra_cookies", "")))
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, cookies=cookies,
            http2=bool(api.get("http2", True)), timeout=httpx.Timeout(30.0),
        )
        self._host = httpx.URL(self.base_url).host
        self._session = api.get("session_cookie", "")
        # Optional callback invoked if the session cookie is refreshed (persistence).
        self.session_sink = None

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    #  Session (wtcg_session cookie) — live update
    # ------------------------------------------------------------------ #
    def _read_cookie(self, name: str) -> str | None:
        """Read a cookie tolerating DUPLICATES (several domains/paths): otherwise httpx
        raises CookieConflict. We prefer the value bound to the host."""
        try:
            return self._client.cookies.get(name)
        except httpx.CookieConflict:
            chosen = None
            for ck in self._client.cookies.jar:
                if ck.name == name:
                    chosen = ck.value
                    dom = (ck.domain or "").lstrip(".")
                    if dom and (self._host == dom or self._host.endswith(dom)):
                        return ck.value
            return chosen

    @property
    def session_cookie(self) -> str:
        return self._read_cookie("wtcg_session") or self._session

    def _set_cookie(self, name: str, value: str) -> None:
        """Cleanly replace a cookie: first remove ALL of its variants (otherwise httpx
        accumulates several cookies with the same name -> CookieConflict)."""
        try:
            self._client.cookies.delete(name)
        except Exception:
            pass
        # safety net: manually purge any remaining variant in the jar
        for ck in [c for c in self._client.cookies.jar if c.name == name]:
            self._client.cookies.jar.clear(ck.domain, ck.path, ck.name)
        self._client.cookies.set(name, value)   # default domain, same as at initialization

    def update_session(self, session_cookie: str, extra_cookies: str = "") -> None:
        """Replace the session cookie(s) WITHOUT recreating the client (takes effect immediately).
        Only two cookies to manage: wtcg_session and (optionally) cf_clearance."""
        self._set_cookie("wtcg_session", session_cookie)
        for k, v in _parse_cookies(extra_cookies).items():
            self._set_cookie(k, v)
        self._session = session_cookie
        log.info("Session cookie(s) updated (takes effect immediately).")

    # ------------------------------------------------------------------ #
    #  Core: request + retry/backoff
    # ------------------------------------------------------------------ #
    async def _request(self, method: str, path: str, *, json_body: Any = None,
                       params: dict | None = None, heavy: bool = False,
                       retry_5xx: bool = True, retry_429: bool = True) -> Any:
        attempt = 0
        mode = "read" if method.upper() == "GET" else "action"
        body_log = "" if json_body is None else f" body={json_body}"
        while True:
            await self.throttle.wait(mode=mode, heavy=heavy)
            log.debug("→ %s %s%s%s", method, path, f" attempt={attempt+1}" if attempt else "", body_log)
            t0 = time.monotonic()
            try:
                resp = await self._client.request(method, path, json=json_body, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                attempt += 1
                log.warning("✗ %s %s — network %s (attempt %d/%d)", method, path,
                            exc.__class__.__name__, attempt, self.retry_cfg["max_retries"])
                if attempt > self.retry_cfg["max_retries"]:
                    raise ApiError(f"Network unavailable after {attempt} attempts: {exc}") from exc
                await self._backoff(attempt, reason=f"network ({exc.__class__.__name__})")
                continue

            status = resp.status_code
            ms = (time.monotonic() - t0) * 1000
            log.debug("← %s %s %d (%.0f ms)", method, path, status, ms)

            # Defensive: if the server returns a refreshed session cookie, capture it.
            fresh = self._read_cookie("wtcg_session")
            if fresh and fresh != self._session:
                self._session = fresh
                log.info("Session cookie refreshed by the server.")
                if self.session_sink:
                    try:
                        self.session_sink(fresh)
                    except Exception:
                        log.warning("session_sink failed", exc_info=True)

            if status == 429:
                if not retry_429:   # e.g. recycling: a 429 = daily quota reached, no point insisting
                    log.warning("⚠ %s %s → 429 (no retry — quota?) — %s", method, path, resp.text[:200])
                    raise ApiError("Recycle quota/limit reached (429)", status=429,
                                   body=resp.text[:300])
                attempt += 1
                if attempt > self.retry_cfg["max_retries"]:
                    raise ApiError("Persistent 429: rate limit not lifted", status=429)
                ra = resp.headers.get("Retry-After")
                retry_after = float(ra) if (ra and ra.isdigit()) else None
                await self._backoff(attempt, reason="429 rate-limit", retry_after=retry_after)
                continue

            if status in (401, 403):
                log.error("⛔ %s %s → %d: authentication rejected — %s",
                          method, path, status, resp.text[:200])
                raise AuthError("Authentication rejected (wtcg_session cookie expired?)",
                                status=status, body=resp.text[:300])

            if 500 <= status < 600:
                if not retry_5xx:   # e.g. card-by-card recycling: skip the failing one quickly
                    log.warning("⚠ %s %s → %d (no retry) — %s", method, path, status, resp.text[:200])
                    raise ApiError(f"Server error {status}", status=status, body=resp.text[:300])
                attempt += 1
                log.warning("⚠ %s %s → %d (attempt %d/%d) — %s", method, path, status,
                            attempt, self.retry_cfg["max_retries"], resp.text[:200])
                if attempt > self.retry_cfg["max_retries"]:
                    raise ApiError(f"Persistent server error {status}", status=status,
                                   body=resp.text[:300])
                await self._backoff(attempt, reason=f"server {status}")
                continue

            if status >= 400:
                log.warning("⚠ %s %s → %d: %s", method, path, status, resp.text[:200])
                raise ApiError(f"API error {status} on {path}", status=status, body=resp.text[:300])

            # 2xx — actions (POST) are logged at INFO, reads stay at DEBUG.
            if mode == "action":
                log.info("✓ %s %s → %d (%.0f ms)", method, path, status, ms)
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text

    async def _backoff(self, attempt: int, *, reason: str, retry_after: float | None = None) -> None:
        if retry_after is not None:
            delay = retry_after
        else:
            delay = min(self.retry_cfg["backoff_base"] ** attempt, self.retry_cfg["backoff_cap"])
            delay *= 1 + random.uniform(0, self.retry_cfg["backoff_jitter"])
        log.warning("Backoff (%s) — attempt %d, waiting %.1f s", reason, attempt, delay)
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------ #
    #  Endpoints — lecture
    # ------------------------------------------------------------------ #
    async def get_status(self) -> dict:
        return await self._request("GET", "/api/nav/status")

    async def get_collection(self) -> list[dict]:
        return await self._request("GET", "/api/collection")

    async def get_series_detail(self, series_id: str) -> list[dict]:
        return await self._request("GET", f"/api/collection/{series_id}")

    async def get_series_catalog(self, series_id: str) -> dict:
        return await self._request("GET", f"/api/series/{series_id}/cards")

    async def marketplace_mine(self) -> dict:
        return await self._request("GET", "/api/marketplace/mine")

    async def marketplace_browse(self, params: dict | None = None) -> dict:
        return await self._request("GET", "/api/marketplace", params=params or {})

    # ------------------------------------------------------------------ #
    #  Endpoints — write
    # ------------------------------------------------------------------ #
    async def open_pack(self, series_id: str) -> dict:
        return await self._request("POST", "/api/packs/open",
                                   json_body={"seriesId": series_id}, heavy=True)

    async def recycle(self, pull_ids: list[str], *, retry_5xx: bool = True,
                      retry_429: bool = True) -> dict:
        # /api/cards/recycle expects the "cardIds" key but with COPY ids (pullIds).
        # Response: {"recycled", "inkEarned", "newBalance"}. `retry_5xx=False` for card-by-card
        # recycling: skip a card that returns 500 right away.
        # `retry_429=False`: a 429 here = daily recycle quota reached -> raise immediately
        # (the engine pauses recycling instead of hammering).
        return await self._request("POST", "/api/cards/recycle",
                                   json_body={"cardIds": pull_ids},
                                   retry_5xx=retry_5xx, retry_429=retry_429)

    async def regen_packs(self, type_: str = "full") -> dict:
        # Buy a restock with ink. Response: {success, newBalance, freePacks, totalAvailable}.
        return await self._request("POST", "/api/packs/regen", json_body={"type": type_})

    async def marketplace_create(self, offered_type: str, offered_series: str,
                                 wanted_card: str, wanted_series: str) -> dict:
        # Contract confirmed via the site frontend: we offer a card TYPE
        # (offeredCardTypeId = card_id); the server picks the copy to commit.
        return await self._request("POST", "/api/marketplace/create", json_body={
            "offeredCardTypeId": offered_type, "offeredSeriesId": offered_series,
            "wantedCardId": wanted_card, "wantedSeriesId": wanted_series})

    async def marketplace_fulfill(self, listing_id: str) -> dict:
        return await self._request("POST", f"/api/marketplace/{listing_id}/fulfill")

    async def marketplace_cancel(self, listing_id: str) -> dict:
        return await self._request("POST", f"/api/marketplace/{listing_id}/cancel")

    # ------------------------------------------------------------------ #
    #  Duplicates (recycling) — GET /api/cards/duplicates (limited batch, global)
    # ------------------------------------------------------------------ #
    async def get_duplicates(self, recycle_cfg: dict) -> list[dict]:
        """Return a normalized list of duplicates:
            [{card_id, series_id, rarity, copies, pull_ids:[...], title}]

        Actual shape of /api/cards/duplicates (one row per card type):
            {"duplicates": [{"cardId","seriesId","rarity","copies","pullIds":[…],"title"}]}
        each `pullIds[i]` is the hex id of a COPY (that is what /recycle expects).

        Parsing stays tolerant: we auto-detect field names and accept a single copy id
        instead of a list (config = explicit override).
        """
        ep = recycle_cfg.get("duplicates_endpoint", "/api/cards/duplicates")
        if not ep:
            return []
        data = await self._request("GET", ep)
        items = _extract_list(data, recycle_cfg.get("list_key", "") or "duplicates")
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            card_id = _pick(it, recycle_cfg.get("type_field", ""),
                            ["cardId", "card_id", "cardTypeId", "type", "card_type"])
            # copies: `pullIds` list (real) or variants; otherwise a single id is tolerated.
            pull_ids = (it.get("pullIds") or it.get("pull_ids")
                        or it.get("instanceIds") or it.get("instance_ids"))
            if not pull_ids:
                single = _pick(it, recycle_cfg.get("id_field", ""),
                               ["instance_id", "instanceId", "cardInstanceId", "instance", "id"])
                pull_ids = [single] if single else []
            if not isinstance(pull_ids, list):
                pull_ids = [pull_ids]
            pull_ids = [p for p in pull_ids if p]
            if not pull_ids:
                continue
            copies = _pick(it, recycle_cfg.get("quantity_field", ""),
                           ["copies", "quantity", "count", "duplicateCount", "total"])
            out.append({
                "card_id": card_id,
                "series_id": it.get("seriesId") or it.get("series_id") or it.get("series"),
                "rarity": it.get("rarity"),
                "copies": int(copies) if isinstance(copies, (int, float)) else len(pull_ids),
                "pull_ids": pull_ids,
                "title": it.get("title"),
            })
        return out
