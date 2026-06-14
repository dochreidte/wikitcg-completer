"""Tests des helpers purs (parsing API, cookies, JWT)."""
import base64
import json
import time
import unittest

from app import api_client, auth


class ExtractList(unittest.TestCase):
    def test_list_passthrough(self):
        self.assertEqual(api_client._extract_list([1, 2]), [1, 2])

    def test_explicit_key(self):
        self.assertEqual(api_client._extract_list({"duplicates": [1]}, "duplicates"), [1])

    def test_autodetect_known_keys(self):
        self.assertEqual(api_client._extract_list({"cards": [1, 2]}), [1, 2])

    def test_empty_when_unknown(self):
        self.assertEqual(api_client._extract_list({"foo": 1}), [])


class Pick(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(api_client._pick({"a": 1, "b": 2}, "b", ["a"]), 2)

    def test_falls_back_to_candidates(self):
        self.assertEqual(api_client._pick({"cardId": "x"}, "", ["card_id", "cardId"]), "x")

    def test_none_when_absent(self):
        self.assertIsNone(api_client._pick({}, "", ["a", "b"]))


class ParseCookies(unittest.TestCase):
    def test_parses_pairs_and_drops_session(self):
        out = api_client._parse_cookies("cf_clearance=abc; other=def; wtcg_session=ignored")
        self.assertEqual(out, {"cf_clearance": "abc", "other": "def"})

    def test_empty(self):
        self.assertEqual(api_client._parse_cookies(""), {})


def _make_jwt(payload: dict) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b64({'alg':'HS256'})}.{b64(payload)}.sig"


class TokenStatus(unittest.TestCase):
    def test_valid_future_token(self):
        tok = _make_jwt({"email": "x@y.z", "exp": int(time.time()) + 3600})
        st = auth.token_status(tok)
        self.assertTrue(st["present"])
        self.assertFalse(st["expired"])
        self.assertGreater(st["expires_in"], 0)
        self.assertEqual(st["email"], "x@y.z")

    def test_expired_token(self):
        tok = _make_jwt({"exp": int(time.time()) - 10})
        st = auth.token_status(tok)
        self.assertTrue(st["expired"])

    def test_placeholder_not_present(self):
        self.assertFalse(auth.token_status("PASTE_YOUR_TOKEN")["present"])
        self.assertFalse(auth.token_status("")["present"])


if __name__ == "__main__":
    unittest.main()
