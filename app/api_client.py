"""Client API asynchrone pour wikitcg.net.

Conçu pour être robuste et extensible :
  * un throttle global (intervalle minimal + jitter) entre TOUTES les requêtes ;
  * un backoff exponentiel + jitter sur 429 / 5xx / erreurs réseau ;
  * une taxonomie d'erreurs claire pour décider du comportement de retry ;
  * journalisation systématique.

Ajouter un nouvel endpoint = ajouter une petite méthode qui appelle self._request().
Ajouter une nouvelle règle d'erreur = l'aiguiller dans _classify() / _request().
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
    """Trouve le tableau d'éléments dans une réponse, avec clé explicite ou autodétection."""
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
    """Renvoie la 1re valeur trouvée : champ explicite (config) sinon candidats par défaut."""
    if explicit and explicit in item:
        return item[explicit]
    for c in candidates:
        if c in item and item[c] not in (None, ""):
            return item[c]
    return None


def _parse_cookies(raw: str) -> dict:
    """Parse une chaîne d'en-tête Cookie ('k=v; k2=v2') en dict. Vide -> {}."""
    out: dict = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            if k and k != "wtcg_session":   # le jwt est géré séparément
                out[k] = v.strip()
    return out


# --------------------------------------------------------------------------- #
#  Taxonomie d'erreurs
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    """Erreur applicative non récupérable (4xx hors auth/429)."""
    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class AuthError(ApiError):
    """401/403 — cookie expiré / non autorisé. On arrête, ça ne se règle pas par un retry."""


class RateLimited(Exception):
    """429 — interne, déclenche le backoff."""
    def __init__(self, retry_after: float | None = None):
        super().__init__("429 Too Many Requests")
        self.retry_after = retry_after


# --------------------------------------------------------------------------- #
#  Throttle global
# --------------------------------------------------------------------------- #
class Throttle:
    """Espacement entre requêtes. Deux régimes :
      * lectures (GET) : court délai (la synchro reste rapide) ;
      * actions (POST) : délai plus long + pause périodique (anti-429 sur les rafales d'ouverture).
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
        """`mode="read"` pour les GET (rapide), `"action"` pour les POST.
        `heavy=True` compte l'action dans le cooldown (ex. ouverture de pack)."""
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
                    log.info("Cooldown anti-429 : pause de %.0f s", pause)
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
        # Callback optionnel appelé si le cookie de session est rafraîchi (persistance).
        self.session_sink = None

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    #  Session (cookie wtcg_session) — mise à jour en direct
    # ------------------------------------------------------------------ #
    def _read_cookie(self, name: str) -> str | None:
        """Lit un cookie en tolérant les DOUBLONS (plusieurs domaines/chemins) : httpx lève
        sinon CookieConflict. On renvoie de préférence la valeur liée au host."""
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
        """Remplace proprement un cookie : on retire d'abord TOUTES ses variantes (sinon httpx
        accumule plusieurs cookies de même nom -> CookieConflict)."""
        try:
            self._client.cookies.delete(name)
        except Exception:
            pass
        # filet : purge manuelle de toute variante restante dans le jar
        for ck in [c for c in self._client.cookies.jar if c.name == name]:
            self._client.cookies.jar.clear(ck.domain, ck.path, ck.name)
        self._client.cookies.set(name, value)   # domaine par défaut, comme à l'initialisation

    def update_session(self, session_cookie: str, extra_cookies: str = "") -> None:
        """Remplace le(s) cookie(s) de session SANS recréer le client (effet immédiat).
        Il n'y a que deux cookies à gérer : wtcg_session et (optionnel) cf_clearance."""
        self._set_cookie("wtcg_session", session_cookie)
        for k, v in _parse_cookies(extra_cookies).items():
            self._set_cookie(k, v)
        self._session = session_cookie
        log.info("Cookie(s) de session mis à jour (effet immédiat).")

    # ------------------------------------------------------------------ #
    #  Cœur : requête + retry/backoff
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
                log.warning("✗ %s %s — réseau %s (essai %d/%d)", method, path,
                            exc.__class__.__name__, attempt, self.retry_cfg["max_retries"])
                if attempt > self.retry_cfg["max_retries"]:
                    raise ApiError(f"Réseau indisponible après {attempt} essais : {exc}") from exc
                await self._backoff(attempt, reason=f"réseau ({exc.__class__.__name__})")
                continue

            status = resp.status_code
            ms = (time.monotonic() - t0) * 1000
            log.debug("← %s %s %d (%.0f ms)", method, path, status, ms)

            # Défensif : si le serveur renvoie un cookie de session rafraîchi, on le capte.
            fresh = self._read_cookie("wtcg_session")
            if fresh and fresh != self._session:
                self._session = fresh
                log.info("Cookie de session rafraîchi par le serveur.")
                if self.session_sink:
                    try:
                        self.session_sink(fresh)
                    except Exception:
                        log.warning("session_sink a échoué", exc_info=True)

            if status == 429:
                if not retry_429:   # ex. recyclage : un 429 = quota journalier atteint, inutile d'insister
                    log.warning("⚠ %s %s → 429 (sans retry — quota ?) — %s", method, path, resp.text[:200])
                    raise ApiError("Quota/limite de recyclage atteint (429)", status=429,
                                   body=resp.text[:300])
                attempt += 1
                if attempt > self.retry_cfg["max_retries"]:
                    raise ApiError("429 persistant : limite de débit non levée", status=429)
                ra = resp.headers.get("Retry-After")
                retry_after = float(ra) if (ra and ra.isdigit()) else None
                await self._backoff(attempt, reason="429 rate-limit", retry_after=retry_after)
                continue

            if status in (401, 403):
                log.error("⛔ %s %s → %d : authentification refusée — %s",
                          method, path, status, resp.text[:200])
                raise AuthError("Authentification refusée (cookie wtcg_session expiré ?)",
                                status=status, body=resp.text[:300])

            if 500 <= status < 600:
                if not retry_5xx:   # ex. recyclage carte par carte : on saute vite la fautive
                    log.warning("⚠ %s %s → %d (sans retry) — %s", method, path, status, resp.text[:200])
                    raise ApiError(f"Erreur serveur {status}", status=status, body=resp.text[:300])
                attempt += 1
                log.warning("⚠ %s %s → %d (essai %d/%d) — %s", method, path, status,
                            attempt, self.retry_cfg["max_retries"], resp.text[:200])
                if attempt > self.retry_cfg["max_retries"]:
                    raise ApiError(f"Erreur serveur {status} persistante", status=status,
                                   body=resp.text[:300])
                await self._backoff(attempt, reason=f"serveur {status}")
                continue

            if status >= 400:
                log.warning("⚠ %s %s → %d : %s", method, path, status, resp.text[:200])
                raise ApiError(f"Erreur API {status} sur {path}", status=status, body=resp.text[:300])

            # 2xx — les actions (POST) sont notées en INFO, les lectures restent en DEBUG.
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
        log.warning("Backoff (%s) — essai %d, attente %.1f s", reason, attempt, delay)
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
    #  Endpoints — écriture
    # ------------------------------------------------------------------ #
    async def open_pack(self, series_id: str) -> dict:
        return await self._request("POST", "/api/packs/open",
                                   json_body={"seriesId": series_id}, heavy=True)

    async def recycle(self, pull_ids: list[str], *, retry_5xx: bool = True,
                      retry_429: bool = True) -> dict:
        # /api/cards/recycle attend la clé "cardIds" mais des id d'EXEMPLAIRES (pullIds).
        # Réponse : {"recycled", "inkEarned", "newBalance"}. `retry_5xx=False` pour le
        # recyclage carte par carte : on saute aussitôt une carte qui renvoie 500.
        # `retry_429=False` : un 429 ici = quota journalier de recyclage atteint -> on lève
        # tout de suite (le moteur met le recyclage en pause au lieu de marteler).
        return await self._request("POST", "/api/cards/recycle",
                                   json_body={"cardIds": pull_ids},
                                   retry_5xx=retry_5xx, retry_429=retry_429)

    async def regen_packs(self, type_: str = "full") -> dict:
        # Achat d'un restock à l'encre. Réponse : {success, newBalance, freePacks, totalAvailable}.
        return await self._request("POST", "/api/packs/regen", json_body={"type": type_})

    async def marketplace_create(self, offered_type: str, offered_series: str,
                                 wanted_card: str, wanted_series: str) -> dict:
        # Contrat confirmé via le frontend du site : on offre un TYPE de carte
        # (offeredCardTypeId = card_id) ; le serveur choisit l'exemplaire à engager.
        return await self._request("POST", "/api/marketplace/create", json_body={
            "offeredCardTypeId": offered_type, "offeredSeriesId": offered_series,
            "wantedCardId": wanted_card, "wantedSeriesId": wanted_series})

    async def marketplace_fulfill(self, listing_id: str) -> dict:
        return await self._request("POST", f"/api/marketplace/{listing_id}/fulfill")

    async def marketplace_cancel(self, listing_id: str) -> dict:
        return await self._request("POST", f"/api/marketplace/{listing_id}/cancel")

    # ------------------------------------------------------------------ #
    #  Doublons (recyclage) — GET /api/cards/duplicates (lot limité, global)
    # ------------------------------------------------------------------ #
    async def get_duplicates(self, recycle_cfg: dict) -> list[dict]:
        """Renvoie une liste normalisée de doublons :
            [{card_id, series_id, rarity, copies, pull_ids:[...], title}]

        Forme réelle de /api/cards/duplicates (une ligne par type de carte) :
            {"duplicates": [{"cardId","seriesId","rarity","copies","pullIds":[…],"title"}]}
        chaque `pullIds[i]` est l'id hex d'un EXEMPLAIRE (c'est ce qu'attend /recycle).

        Le parsing reste tolérant : on autodétecte les noms de champs et on accepte
        un id d'exemplaire unique au lieu d'une liste (config = override explicite).
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
            # exemplaires : liste `pullIds` (réel) ou variantes ; sinon id unique toléré.
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
