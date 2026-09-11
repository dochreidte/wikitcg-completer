"""JWT decoding and session helpers."""
import base64
import json
import time
import unittest

from app.auth import decode_jwt, is_real_session, token_status, PLACEHOLDER


def _make_jwt(payload: dict) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b64({'alg':'HS256'})}.{b64(payload)}.sig"


class DecodeJWT(unittest.TestCase):
    def test_valid_token(self):
        tok = _make_jwt({"email": "test@example.com", "exp": 12345})
        payload = decode_jwt(tok)
        self.assertEqual(payload["email"], "test@example.com")
        self.assertEqual(payload["exp"], 12345)

    def test_missing_parts(self):
        self.assertEqual(decode_jwt("noparts"), {})
        self.assertEqual(decode_jwt("only.two"), {})

    def test_invalid_base64(self):
        self.assertEqual(decode_jwt("x.!!!invalid!!!.sig"), {})

    def test_invalid_json(self):
        bad_payload = base64.urlsafe_b64encode(b"not json").rstrip(b"=").decode()
        self.assertEqual(decode_jwt(f"x.{bad_payload}.sig"), {})

    def test_none_token(self):
        self.assertEqual(decode_jwt(""), {})

    def test_empty_string(self):
        self.assertEqual(decode_jwt(""), {})

    def test_padding_not_required(self):
        payload_dict = {"exp": 9999999999}
        b64_payload = base64.urlsafe_b64encode(
            json.dumps(payload_dict).encode()
        ).rstrip(b"=").decode()
        self.assertFalse(b64_payload.endswith("="))
        tok = f"x.{b64_payload}.sig"
        decoded = decode_jwt(tok)
        self.assertEqual(decoded["exp"], 9999999999)


class IsRealSession(unittest.TestCase):
    def test_valid_session(self):
        self.assertTrue(is_real_session("eyJ.payload.sig"))

    def test_placeholder_false(self):
        self.assertFalse(is_real_session("PASTE_YOUR_TOKEN_HERE"))
        self.assertFalse(is_real_session("prefix_PASTE_YOUR_suffix"))

    def test_empty_false(self):
        self.assertFalse(is_real_session(""))

    def test_none_string_false(self):
        pass


class TokenStatus(unittest.TestCase):
    def test_valid_future_token(self):
        exp = int(time.time()) + 3600
        tok = _make_jwt({"email": "x@y.z", "name": "Test User", "exp": exp})
        status = token_status(tok)
        self.assertTrue(status["present"])
        self.assertFalse(status["expired"])
        self.assertGreater(status["expires_in"], 0)
        self.assertEqual(status["email"], "x@y.z")
        self.assertEqual(status["name"], "Test User")
        self.assertEqual(status["exp"], exp)

    def test_expired_token(self):
        exp = int(time.time()) - 10
        tok = _make_jwt({"exp": exp})
        status = token_status(tok)
        self.assertTrue(status["present"])
        self.assertTrue(status["expired"])
        self.assertLess(status["expires_in"], 0)

    def test_no_expiry_field(self):
        tok = _make_jwt({"email": "test@example.com"})
        status = token_status(tok)
        self.assertTrue(status["present"])
        self.assertIsNone(status["exp"])
        self.assertIsNone(status["expires_in"])
        self.assertIsNone(status["expired"])

    def test_placeholder_token(self):
        status = token_status("PASTE_YOUR_TOKEN_HERE")
        self.assertFalse(status["present"])

    def test_empty_token(self):
        status = token_status("")
        self.assertFalse(status["present"])
        self.assertIsNone(status["email"])
        self.assertIsNone(status["exp"])

    def test_none_token_converts_to_empty(self):
        status = token_status(None)  # type: ignore
        self.assertFalse(status["present"])


if __name__ == "__main__":
    unittest.main()
