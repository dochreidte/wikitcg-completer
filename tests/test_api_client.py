"""Tests for API client with mocked HTTP transport."""
import unittest

import httpx

from app.api_client import WikiTCGClient, ApiError, AuthError
from app.config import Settings, _DEFAULTS, _deep_merge


def _client(handler):
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


class SeriesList(unittest.IsolatedAsyncioTestCase):
    async def test_lists_every_series(self):
        def handler(req):
            if req.url.path != "/api/series":
                return httpx.Response(404)
            return httpx.Response(200, json=[{"id": "ocean-life", "name": "Ocean Life",
                                              "cardCount": 200}])
        c = _client(handler)
        try:
            out = await c.get_series_list()
            self.assertEqual([(s["id"], s["cardCount"]) for s in out], [("ocean-life", 200)])
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
            self.assertEqual(calls["n"], 1)
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
            self.assertEqual(calls["n"], 2)
        finally:
            await c.aclose()

    async def test_429_with_retry_after_integer(self):
        """Retry-After header with integer seconds is respected."""
        calls = {"n": 0}
        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, text="rate")
            return httpx.Response(200, json={"status": "ok"})
        c = _client(handler)
        try:
            await c.get_status()
            self.assertEqual(calls["n"], 2)
        finally:
            await c.aclose()

    async def test_429_with_retry_after_float(self):
        """Retry-After header with fractional seconds is respected."""
        calls = {"n": 0}
        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0.5"}, text="rate")
            return httpx.Response(200, json={"status": "ok"})
        c = _client(handler)
        try:
            await c.get_status()
            self.assertEqual(calls["n"], 2)
        finally:
            await c.aclose()

    async def test_429_without_retry_after_uses_backoff(self):
        """429 without Retry-After uses exponential backoff."""
        calls = {"n": 0}
        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, text="rate")
            return httpx.Response(200, json={"status": "ok"})
        c = _client(handler)
        try:
            await c.get_status()
            self.assertEqual(calls["n"], 2)
        finally:
            await c.aclose()

    async def test_429_with_retry_after_inf_falls_back_to_backoff(self):
        """Retry-After header with 'inf' is rejected; exponential backoff is used."""
        calls = {"n": 0}
        backoff_delays = []

        async def mock_backoff(attempt, *, reason, retry_after=None):
            backoff_delays.append(retry_after)

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "inf"}, text="rate")
            return httpx.Response(200, json={"status": "ok"})
        c = _client(handler)
        try:
            original_backoff = c._backoff
            c._backoff = mock_backoff
            await c.get_status()
            self.assertEqual(calls["n"], 2)
            self.assertEqual(len(backoff_delays), 1)
            self.assertIsNone(backoff_delays[0])
        finally:
            await c.aclose()

    async def test_429_with_retry_after_nan_falls_back_to_backoff(self):
        """Retry-After header with 'nan' is rejected; exponential backoff is used."""
        calls = {"n": 0}
        backoff_delays = []

        async def mock_backoff(attempt, *, reason, retry_after=None):
            backoff_delays.append(retry_after)

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "nan"}, text="rate")
            return httpx.Response(200, json={"status": "ok"})
        c = _client(handler)
        try:
            c._backoff = mock_backoff
            await c.get_status()
            self.assertEqual(calls["n"], 2)
            self.assertEqual(len(backoff_delays), 1)
            self.assertIsNone(backoff_delays[0])
        finally:
            await c.aclose()

    async def test_429_with_retry_after_negative_falls_back_to_backoff(self):
        """Retry-After header with negative value is rejected; exponential backoff is used."""
        calls = {"n": 0}
        backoff_delays = []

        async def mock_backoff(attempt, *, reason, retry_after=None):
            backoff_delays.append(retry_after)

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "-5"}, text="rate")
            return httpx.Response(200, json={"status": "ok"})
        c = _client(handler)
        try:
            c._backoff = mock_backoff
            await c.get_status()
            self.assertEqual(calls["n"], 2)
            self.assertEqual(len(backoff_delays), 1)
            self.assertIsNone(backoff_delays[0])
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
