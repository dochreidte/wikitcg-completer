import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.netguard import host_allowed, install, origin_allowed, request_allowed

LOCAL = "127.0.0.1:8765"


class HostAllowed(unittest.TestCase):
    def test_local_hosts(self):
        for h in ("127.0.0.1:8765", "localhost", "LOCALHOST:1", "[::1]:8765"):
            self.assertTrue(host_allowed(h), h)

    def test_foreign_or_malformed_hosts(self):
        for h in (None, "", "evil.com", "127.0.0.1.evil.com", "localhost.evil.com:8765", "[::1"):
            self.assertFalse(host_allowed(h), h)

    def test_bind_host_is_allowed(self):
        self.assertTrue(host_allowed("mybox.lan:8765", "mybox.lan"))
        self.assertFalse(host_allowed("mybox.lan:8765", None))


class OriginAllowed(unittest.TestCase):
    def test_missing_origin_allowed(self):
        self.assertTrue(origin_allowed(None, LOCAL))
        self.assertTrue(origin_allowed("", LOCAL))

    def test_same_origin_allowed(self):
        self.assertTrue(origin_allowed("http://127.0.0.1:8765", LOCAL))
        self.assertTrue(origin_allowed("HTTP://127.0.0.1:8765", LOCAL))

    def test_cross_origin_rejected(self):
        for o in ("http://evil.com", "http://localhost.evil.com", "http://127.0.0.1:9999",
                  "null", "file://", "ftp://127.0.0.1:8765"):
            self.assertFalse(origin_allowed(o, LOCAL), o)

    def test_origin_without_host_rejected(self):
        self.assertFalse(origin_allowed("http://127.0.0.1:8765", None))


class RequestAllowed(unittest.TestCase):
    def test_safe_methods_skip_origin_check(self):
        self.assertTrue(request_allowed("GET", {"host": LOCAL, "origin": "http://evil.com"}))

    def test_unsafe_methods_check_origin(self):
        self.assertFalse(request_allowed("POST", {"host": LOCAL, "origin": "http://evil.com"}))
        self.assertTrue(request_allowed("POST", {"host": LOCAL, "origin": "http://127.0.0.1:8765"}))

    def test_websocket_checks_origin(self):
        self.assertFalse(request_allowed("WEBSOCKET", {"host": LOCAL, "origin": "http://evil.com"}))

    def test_bad_host_rejected_for_all_methods(self):
        self.assertFalse(request_allowed("GET", {"host": "evil.com"}))


class Install(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        install(app, lambda: None)

        @app.get("/r")
        def r():
            return {"ok": True}

        @app.post("/w")
        def w():
            return {"ok": True}

        self.c = TestClient(app, base_url="http://127.0.0.1:8765")

    def test_same_origin_passes(self):
        self.assertEqual(self.c.post("/w", headers={"origin": "http://127.0.0.1:8765"}).status_code, 200)
        self.assertEqual(self.c.get("/r").status_code, 200)

    def test_cross_origin_post_forbidden(self):
        self.assertEqual(self.c.post("/w", headers={"origin": "http://evil.com"}).status_code, 403)

    def test_dns_rebinding_forbidden(self):
        self.assertEqual(self.c.get("/r", headers={"host": "evil.com"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
