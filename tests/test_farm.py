import asyncio
import base64
import json
import os
import shutil
import tempfile
import time
import unittest

import keyring

from app import vault
from app.config import load_settings
from app.db import Database
from app.events import EventBus
from app.farm import Farm
from tests.fakes import FakeClient, FakeEngine, MemoryKeyring


def make_jwt(exp: int) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'HS256'})}.{enc({'exp': exp, 'email': 'a@b.c'})}.sig"


async def until(cond, timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


class FarmTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.prev_ring = keyring.get_keyring()
        keyring.set_keyring(MemoryKeyring())
        self.app_db = Database(os.path.join(self.tmp, "app.db"))
        self.settings = load_settings()
        self.bus = EventBus()
        self.engines: list[FakeEngine] = []
        self.farm = self.make_farm()

    def make_farm(self) -> Farm:
        def engine_factory(*args):
            eng = FakeEngine(*args)
            self.engines.append(eng)
            return eng

        return Farm(self.app_db, self.settings, self.bus,
                    client_factory=lambda s: FakeClient(), engine_factory=engine_factory,
                    db_factory=lambda p: Database(os.path.join(self.tmp, p)),
                    poll_seconds=0.01, idle_cycle_seconds=0.05)

    async def asyncTearDown(self):
        await self.farm.close()
        self.app_db.close()
        keyring.set_keyring(self.prev_ring)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def current(self) -> str | None:
        return self.farm.session.account["id"] if self.farm.session else None

    async def test_activate_builds_account_settings(self):
        await self.farm.add("a1", "space-cosmos")
        await self.farm.add("a2", "")
        vault.store("a1", "session", "eyJ.a.b")
        await self.farm.activate("a1")
        s = self.farm.session.engine.settings
        self.assertEqual(s.engine["only_series"], "space-cosmos")
        self.assertEqual(s.engine["only_series_mode"], "strict")
        self.assertEqual(s.api["session_cookie"], "eyJ.a.b")
        self.assertEqual(s.paths["database"], "wikitcg_a1.db")
        await self.farm.activate("a2")
        s = self.farm.session.engine.settings
        self.assertEqual((s.engine["only_series"], s.engine["only_series_mode"]), ("", "fallback"))
        with self.assertRaises(KeyError):
            await self.farm.activate("missing")

    async def test_activate_publishes_account_event(self):
        await self.farm.add("a1", "")
        q = self.bus.subscribe()
        await self.farm.activate("a1")
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        self.assertIn({"kind": "account", "current": "a1", "name": "a1"}, events)

    async def test_restore_reopens_last_current_account(self):
        await self.farm.add("a1", "")
        await self.farm.add("a2", "")
        await self.farm.restore()
        self.assertEqual(self.current(), "a1")
        await self.farm.activate("a2")
        await self.farm.close()
        self.farm = self.make_farm()
        await self.farm.restore()
        self.assertEqual(self.current(), "a2")

    async def test_start_without_accounts_is_noop(self):
        self.farm.start()
        self.assertFalse(self.farm.running)

    async def test_single_account_keeps_waiting_engine(self):
        await self.farm.add("a1", "")
        self.farm.start()
        await until(lambda: self.engines and self.engines[0].running)
        self.engines[0].status = "waiting"
        await asyncio.sleep(0.08)
        self.assertEqual(len(self.engines), 1)
        self.assertTrue(self.engines[0].running)
        self.assertTrue(self.farm.running)

    async def test_multi_account_switches_when_exhausted(self):
        await self.farm.add("a1", "")
        await self.farm.add("a2", "")
        self.farm.start()
        await until(lambda: self.engines and self.engines[0].running)
        self.engines[0].status = "waiting"
        await until(lambda: self.current() == "a2" and self.engines[-1].running)
        self.assertFalse(self.engines[0].running)

    async def test_new_cycle_after_all_accounts_exhausted(self):
        await self.farm.add("a1", "")
        await self.farm.add("a2", "")
        self.farm.start()

        def exhaust():
            if self.engines and self.engines[-1].running:
                self.engines[-1].status = "waiting"
            return self.farm.cycle >= 2

        await until(exhaust)

    async def test_error_on_single_account_stops_farm(self):
        await self.farm.add("a1", "")
        self.farm.start()
        await until(lambda: self.engines and self.engines[0].running)
        self.engines[0].status, self.engines[0].running = "error", False
        await until(lambda: not self.farm.running)

    async def test_activate_while_running_switches(self):
        await self.farm.add("a1", "")
        await self.farm.add("a2", "")
        self.farm.start()
        await until(lambda: self.engines and self.engines[0].running)
        await self.farm.activate("a2")
        await until(lambda: self.current() == "a2" and self.engines[-1].running)
        self.assertTrue(self.farm.running)

    async def test_next_rotates_when_stopped(self):
        for name in ("a1", "a2", "a3"):
            await self.farm.add(name, "")
        await self.farm.restore()
        seen = []
        for _ in range(3):
            await self.farm.next()
            seen.append(self.current())
        self.assertEqual(seen, ["a2", "a3", "a1"])

    async def test_stop_stops_engine(self):
        await self.farm.add("a1", "")
        self.farm.start()
        await until(lambda: self.engines and self.engines[0].running)
        await self.farm.stop()
        self.assertFalse(self.farm.running)
        self.assertFalse(self.engines[0].running)
        self.assertEqual(self.current(), "a1")

    async def test_refresh_settings_propagates_to_current_engine(self):
        await self.farm.add("a1", "mystery")
        await self.farm.activate("a1")
        eng_settings = self.farm.session.engine.settings
        self.settings.raw["engine"]["mystery_pack"] = False
        self.farm.refresh_settings()
        self.assertIs(self.farm.session.engine.settings, eng_settings)
        self.assertFalse(eng_settings.engine["mystery_pack"])
        self.assertEqual(eng_settings.engine["only_series"], "mystery")

    async def test_update_current_series_applies_live(self):
        await self.farm.add("a1", "")
        await self.farm.activate("a1")
        acc = await self.farm.update("a1", series="space-cosmos")
        self.assertEqual(acc["series"], "space-cosmos")
        self.assertEqual(self.farm.session.engine.settings.engine["only_series_mode"], "strict")
        self.assertIsNone(await self.farm.update("missing", name="x"))

    async def test_session_sink_stores_refreshed_cookie(self):
        await self.farm.add("a1", "")
        await self.farm.activate("a1")
        self.farm.session.client.session_sink("eyJ.new.tok")
        self.assertEqual(vault.get("a1", "session"), "eyJ.new.tok")

    async def test_set_cookie_updates_vault_and_live_client(self):
        await self.farm.add("a1", "")
        await self.farm.activate("a1")
        self.farm.session.engine.auth_error = "401"
        token = make_jwt(int(time.time()) + 86400)
        status = self.farm.set_cookie("a1", token, "cf=1")
        self.assertTrue(status["present"])
        self.assertEqual(vault.get("a1", "session"), token)
        self.assertEqual(vault.get("a1", "extra"), "cf=1")
        self.assertEqual(self.farm.session.client.session_cookie, token)
        self.assertIsNone(self.farm.session.engine.auth_error)
        with self.assertRaises(KeyError):
            self.farm.set_cookie("missing", token, "")

    async def test_snapshot_exposes_token_status_not_cookie(self):
        await self.farm.add("a1", "space-cosmos")
        token = make_jwt(int(time.time()) + 86400)
        vault.store("a1", "session", token)
        vault.store("a1", "extra", "cf_clearance=zzz")
        await self.farm.restore()
        snap = self.farm.snapshot()
        self.assertNotIn(token, json.dumps(snap))
        self.assertNotIn("zzz", json.dumps(snap))
        row = snap["accounts"][0]
        self.assertEqual((row["id"], row["name"], row["series"], row["current"]),
                         ("a1", "a1", "space-cosmos", True))
        self.assertTrue(row["token"]["present"])
        self.assertEqual(snap["current"], "a1")
        self.assertFalse(snap["running"])
        self.assertIn("available", snap["vault"])

    async def test_remove_current_while_stopped_moves_session(self):
        await self.farm.add("a1", "")
        await self.farm.add("a2", "")
        vault.store("a1", "session", "tok")
        await self.farm.activate("a1")
        self.assertTrue(await self.farm.remove("a1"))
        self.assertEqual(self.current(), "a2")
        self.assertEqual([a["id"] for a in self.app_db.list_accounts()], ["a2"])
        self.assertEqual(vault.get("a1", "session"), "")
        self.assertFalse(await self.farm.remove("a1"))
        await self.farm.remove("a2")
        self.assertIsNone(self.farm.session)

    async def test_remove_current_while_running_rejected(self):
        await self.farm.add("a1", "")
        self.farm.start()
        await until(lambda: self.engines and self.engines[0].running)
        with self.assertRaises(ValueError):
            await self.farm.remove("a1")


if __name__ == "__main__":
    unittest.main()
