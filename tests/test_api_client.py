"""Tests du vrai client HTTP via httpx.MockTransport (sans réseau).
Couvre le parsing des doublons et la taxonomie d'erreurs (429/5xx/401)."""
import unittest

import httpx

from app.api_client import WikiTCGClient, ApiError, AuthError
from app.config import Settings, _DEFAULTS, _deep_merge


def _client(handler):
    """Client avec throttle/backoff quasi nuls + transport simulé."""
    settings = Settings(raw=_deep_merge(_DEFAULTS, {
        "throttle": {"read_interval": 0, "read_jitter": 0, "min_interval": 0, "jitter": 0,
                     "cooldown_every": 0, "cooldown_seconds": 0, "cooldown_jitter": 0},
        "retry": {"max_retries": 3, "backoff_base": 1.0, "backoff_cap": 0.001, "backoff_jitter": 0},
    }))
    c = WikiTCGClient(settings)
    c._client = httpx.AsyncClient(base_url="https://wikitcg.net",
                                  transport=httpx.MockTransport(handler),
                                  cookies={"wtcg_session": "tok"})
    return c


class Duplicates(unittest.IsolatedAsyncioTestCase):
    async def test_parses_pullids_and_copies(self):
        def handler(req):
            return httpx.Response(200, json={"duplicates": [
                {"cardId": "wiki-1", "seriesId": "s", "rarity": "SR", "copies": 3,
                 "pullIds": ["a", "b", "c"], "title": "T"}]})
        c = _client(handler)
        try:
            out = await c.get_duplicates({"duplicates_endpoint": "/api/cards/duplicates"})
            self.assertEqual(len(out), 1)
            d = out[0]
            self.assertEqual(d["card_id"], "wiki-1")
            self.assertEqual(d["copies"], 3)
            self.assertEqual(d["pull_ids"], ["a", "b", "c"])
        finally:
            await c.aclose()


class Recycle(unittest.IsolatedAsyncioTestCase):
    async def test_recycle_ok(self):
        def handler(req):
            return httpx.Response(200, json={"recycled": 1, "inkEarned": 40, "newBalance": 100})
        c = _client(handler)
        try:
            self.assertEqual((await c.recycle(["p"]))["inkEarned"], 40)
        finally:
            await c.aclose()

    async def test_500_no_retry_raises_immediately(self):
        calls = {"n": 0}
        def handler(req):
            calls["n"] += 1
            return httpx.Response(500, text="")
        c = _client(handler)
        try:
            with self.assertRaises(ApiError):
                await c.recycle(["p"], retry_5xx=False)
            self.assertEqual(calls["n"], 1)   # AUCUN retry
        finally:
            await c.aclose()


class ErrorTaxonomy(unittest.IsolatedAsyncioTestCase):
    async def test_429_then_200_retries(self):
        calls = {"n": 0}
        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, text="rate")
            return httpx.Response(200, json={"ink": 5})
        c = _client(handler)
        try:
            self.assertEqual((await c.get_status())["ink"], 5)
            self.assertEqual(calls["n"], 2)   # 1 échec + 1 succès
        finally:
            await c.aclose()

    async def test_401_raises_autherror(self):
        c = _client(lambda req: httpx.Response(401, text="nope"))
        try:
            with self.assertRaises(AuthError):
                await c.get_status()
        finally:
            await c.aclose()

    async def test_5xx_persistent_raises_apierror(self):
        c = _client(lambda req: httpx.Response(503, text="down"))
        try:
            with self.assertRaises(ApiError):
                await c.get_status()
        finally:
            await c.aclose()


if __name__ == "__main__":
    unittest.main()
