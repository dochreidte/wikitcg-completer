import asyncio
import os
import re
import shutil
import tempfile
import time
import unittest
from unittest import mock

import keyring
from fastapi.testclient import TestClient
from keyring.backends import fail
from starlette.websockets import WebSocketDisconnect

from app import vault
from app.config import load_settings
from app.db import Database
from app.events import EventBus
from app.farm import Farm
from app.web import EDITABLE, _apply_overrides, app, state
from tests.fakes import FakeClient, FakeEngine, MemoryKeyring
from tests.test_farm import make_jwt

BASE = "http://127.0.0.1:8765"
WS = "ws://127.0.0.1:8765/ws"
SAME = {"origin": BASE}


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.prev_ring = keyring.get_keyring()
        keyring.set_keyring(MemoryKeyring())
        self.app_db = Database(os.path.join(self.tmp, "app.db"))
        self.settings = load_settings()
        bus = EventBus()
        self.farm = Farm(self.app_db, self.settings, bus,
                         client_factory=lambda s: FakeClient(), engine_factory=FakeEngine,
                         db_factory=lambda p: Database(os.path.join(self.tmp, p)))
        state.update(settings=self.settings, app_db=self.app_db, bus=bus, farm=self.farm)
        self.c = TestClient(app, base_url=BASE)

    def tearDown(self):
        asyncio.run(self.farm.close())
        state.clear()
        self.app_db.close()
        keyring.set_keyring(self.prev_ring)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, path, body=None):
        return self.c.post(path, json=body or {}, headers=SAME)

    def add(self, name="a1", series=""):
        return self.post("/api/accounts", {"name": name, "series": series})

    def test_index_served(self):
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_index_serves_the_six_view_shell(self):
        html = self.c.get("/").text
        self.assertIn('id="theme-switch"', html)
        for view in ("overview", "collection", "accounts", "market", "settings", "log"):
            self.assertIn(f'data-view="{view}"', html)

    def test_every_asset_the_page_references_is_served(self):
        refs = re.findall(r'(?:href|src)="(/assets/[^"]+)"', self.c.get("/").text)
        self.assertTrue(refs, "index.html references no /assets/ files")
        for path in refs:
            self.assertEqual(self.c.get(path).status_code, 200, path)

    def test_assets_cannot_escape_the_frontend_directory(self):
        for path in ("/assets/../app/web.py", "/assets/css/../../../app/web.py"):
            self.assertNotEqual(self.c.get(path).status_code, 200, path)

    def test_state_without_accounts(self):
        d = self.c.get("/api/state").json()
        self.assertIsNone(d["account"])
        self.assertEqual((d["series"], d["actions"], d["running"]), ([], [], False))
        self.assertFalse(d["token"]["present"])
        self.assertEqual(self.c.get("/api/live").status_code, 200)

    def test_add_account_is_shown(self):
        r = self.add("a1", "space-cosmos")
        self.assertEqual(r.json(), {"ok": True, "account": {"id": "a1", "name": "a1", "series": "space-cosmos"}})
        self.assertEqual(self.c.get("/api/state").json()["account"],
                         {"id": "a1", "name": "a1", "series": "space-cosmos"})
        rows = self.c.get("/api/accounts").json()["accounts"]
        self.assertEqual([(a["id"], a["current"]) for a in rows], [("a1", True)])

    def test_add_account_invalid(self):
        r = self.add("", "")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])

    def test_update_and_delete_account(self):
        self.add("a1")
        self.add("a2")
        r = self.post("/api/accounts/a2", {"series": "mystery"})
        self.assertEqual(r.json()["account"]["series"], "mystery")
        self.assertEqual(self.post("/api/accounts/a2", {"series": ""}).json()["account"]["series"], "")
        self.assertEqual(self.post("/api/accounts/nope", {"name": "x"}).status_code, 404)
        self.assertEqual(self.post("/api/accounts/a2", {"series": "bad id"}).status_code, 400)
        self.assertEqual(self.c.delete("/api/accounts/a2", headers=SAME).status_code, 200)
        self.assertEqual(self.c.delete("/api/accounts/a2", headers=SAME).status_code, 404)

    def test_cookie_goes_to_vault_and_is_never_returned(self):
        self.add("a1")
        token = make_jwt(int(time.time()) + 86400)
        r = self.post("/api/accounts/a1/cookie", {"session_cookie": token, "extra_cookies": "cf_clearance=zzz"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["token"]["present"])
        self.assertEqual(vault.get("a1", "session"), token)
        for path in ("/api/state", "/api/live", "/api/accounts", "/api/config"):
            body = self.c.get(path).text
            self.assertNotIn(token, body, path)
            self.assertNotIn("zzz", body, path)

    def test_cookie_errors(self):
        self.add("a1")
        self.assertEqual(self.post("/api/accounts/a1/cookie", {"session_cookie": "abc"}).status_code, 400)
        token = make_jwt(int(time.time()) + 60)
        self.assertEqual(self.post("/api/accounts/zz/cookie", {"session_cookie": token}).status_code, 404)
        self.assertEqual(self.post("/api/accounts/a1/cookie", {"session_cookie": "x." * 5000}).status_code, 400)
        keyring.set_keyring(fail.Keyring())
        r = self.post("/api/accounts/a1/cookie", {"session_cookie": token})
        self.assertEqual(r.status_code, 503)
        self.assertIn("WIKITCG_SESSION_A1", r.json()["error"])
        self.assertNotIn(token, r.text)

    def test_activate_and_next(self):
        self.add("a1")
        self.add("a2")
        self.assertEqual(self.post("/api/accounts/a2/activate").json(), {"ok": True, "current": "a2"})
        self.assertEqual(self.post("/api/control/next").json()["current"], "a1")
        self.assertEqual(self.post("/api/accounts/zz/activate").status_code, 404)

    def test_config_whitelist_validation_and_live_apply(self):
        self.add("a1")
        r = self.post("/api/config", {"mystery_pack": "false", "max_listings": "7", "base_url": "http://evil",
                                      "recycle_mode": "bogus", "min_ink_reserve": "-5", "auto_open": False})
        self.assertEqual(r.json()["applied"], {"mystery_pack": False, "max_listings": 7})
        self.assertFalse(self.farm.session.engine.settings.engine["mystery_pack"])
        self.assertEqual(self.settings.api["base_url"], "https://wikitcg.net")
        self.assertEqual(self.app_db.get_json("settings_overrides"),
                         {"engine": {"mystery_pack": False}, "marketplace": {"max_listings": 7}})
        r = self.post("/api/config/reset")
        self.assertTrue(r.json()["config"]["mystery_pack"])
        self.assertTrue(self.farm.session.engine.settings.engine["mystery_pack"])
        self.assertEqual(self.c.get("/api/state").json()["config_overridden"], [])

    def test_removed_settings_not_editable(self):
        for key in ("auto_open", "on_empty", "dry_run", "idle_poll_seconds",
                    "recycle_quota_cooldown_minutes", "marketplace_min_interval"):
            self.assertNotIn(key, EDITABLE)

    def test_stale_overrides_are_ignored(self):
        self.app_db.set_json("settings_overrides", {"engine": {"on_empty": "stop", "mystery_pack": False},
                                                    "api": {"base_url": "http://evil"}})
        _apply_overrides(self.settings, self.app_db)
        self.assertFalse(self.settings.engine["mystery_pack"])
        self.assertNotIn("on_empty", self.settings.engine)
        self.assertEqual(self.settings.api["base_url"], "https://wikitcg.net")

    def test_logging_persisted_in_app_db(self):
        with mock.patch("app.web.setup_logging"):
            r = self.post("/api/logging", {"level": "debug", "log_requests": True})
        self.assertEqual(r.json()["logging"], {"level": "DEBUG", "log_requests": True})
        self.assertEqual(self.app_db.get_json("logging_override"), {"level": "DEBUG", "log_requests": True})

    def test_control_delegates_to_farm(self):
        with mock.patch.object(self.farm, "start") as start, \
                mock.patch.object(self.farm, "stop", new=mock.AsyncMock()) as stop:
            self.assertTrue(self.post("/api/control/start").json()["ok"])
            self.assertTrue(self.post("/api/control/stop").json()["ok"])
        start.assert_called_once()
        stop.assert_awaited_once()

    def test_account_data_endpoints_without_account(self):
        self.assertEqual(self.post("/api/control/sync").status_code, 409)
        self.assertEqual(self.c.get("/api/stats").json()["history"], [])
        self.assertEqual(self.c.get("/api/marketplace").json()["mine"], [])
        self.assertEqual(self.c.get("/api/series/space-cosmos").status_code, 404)

    def test_account_data_endpoints_with_account(self):
        self.add("a1")
        self.assertIn("history", self.c.get("/api/stats").json())
        self.assertIn("mine", self.c.get("/api/marketplace").json())
        self.assertEqual(self.post("/api/control/sync").json(), {"ok": True})

    def test_cross_origin_and_foreign_host_rejected(self):
        self.assertEqual(self.c.post("/api/accounts", json={"name": "x"},
                                     headers={"origin": "http://evil.com"}).status_code, 403)
        self.assertEqual(self.c.post("/api/control/start", headers={"origin": "http://localhost.evil.com"}).status_code, 403)
        self.assertEqual(self.c.delete("/api/accounts/a1", headers={"origin": "http://evil.com"}).status_code, 403)
        self.assertEqual(self.c.get("/api/state", headers={"host": "evil.com"}).status_code, 403)
        self.assertEqual(self.c.get("/api/accounts").status_code, 200)

    def test_websocket_cross_origin_rejected(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.c.websocket_connect(WS, headers={"origin": "http://evil.com"}) as ws:
                ws.receive_json()

    def test_websocket_same_origin_receives_status_and_events(self):
        with self.c.websocket_connect(WS, headers=SAME) as ws:
            self.assertEqual(ws.receive_json()["kind"], "status")
            self.add("a1")
            self.assertEqual(ws.receive_json(), {"kind": "account", "current": "a1", "name": "a1"})
        self.assertEqual(state["bus"]._subscribers, set())


if __name__ == "__main__":
    unittest.main()
