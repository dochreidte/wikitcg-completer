"""Tests for engine_actions via FakeClient."""
import os
import time
import unittest

from tests.fakes import FakeClient, make_engine, make_settings


def dup(card, rarity, pulls, series="s"):
    return {"card_id": card, "series_id": series, "rarity": rarity,
            "copies": len(pulls), "pull_ids": list(pulls)}


class RecycleMissingPullIds(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_missing_pull_ids_skipped(self):
        client = FakeClient(dups=[
            {"card_id": "wiki-1", "series_id": "s", "rarity": "SR", "copies": 2, "pull_ids": ["p1", "p2"]},
            {"card_id": "wiki-2", "series_id": "s", "rarity": "C", "copies": 1, "pull_ids": None},
        ])
        eng, client, db, path = make_engine(client, keep_spares={"SR": 0})
        try:
            n = await eng.recycle_pass()
            self.assertEqual(n, 1)
            self.assertEqual(client.recycled, ["p1"])
        finally:
            db.close(); os.remove(path)

    async def test_duplicate_missing_pull_ids_key_skipped(self):
        client = FakeClient(dups=[
            {"card_id": "wiki-1", "series_id": "s", "rarity": "SR", "copies": 2},
            {"card_id": "wiki-2", "series_id": "s", "rarity": "C", "copies": 2, "pull_ids": ["p1"]},
        ])
        eng, client, db, path = make_engine(client, keep_spares={"C": 0})
        try:
            n = await eng.recycle_pass()
            self.assertEqual(n, 1)
            self.assertEqual(client.recycled, ["p1"])
        finally:
            db.close(); os.remove(path)


class OpenOneEdgeCases(unittest.IsolatedAsyncioTestCase):
    async def test_open_without_prior_sync_no_total_available(self):
        client = FakeClient(open_result={
            "cards": [{"id": "wiki-a", "rarity": "C", "article": {"title": "A"}}],
            "series": {"name": "S"}})
        eng, client, db, path = make_engine(client)
        try:
            await eng.open_one("s")
            self.assertEqual(eng.resources["total_available"], 0)
            self.assertEqual(eng.resources["free_packs"], 0)
        finally:
            db.close(); os.remove(path)

    async def test_open_with_error_field_in_response(self):
        client = FakeClient(open_result={"error": "no_more_packs"})
        eng, client, db, path = make_engine(client)
        try:
            from app.api_client import ApiError
            with self.assertRaises(ApiError) as ctx:
                await eng.open_one("s")
            self.assertIn("Pack open rejected", str(ctx.exception))
        finally:
            db.close(); os.remove(path)


class MysteryPackLogic(unittest.IsolatedAsyncioTestCase):
    async def test_mystery_pack_due_time_persisted(self):
        import tempfile
        import uuid
        from app.db import Database
        from app.events import EventBus
        from app.engine import Engine

        path = os.path.join(tempfile.gettempdir(), f"_wtcg_{uuid.uuid4().hex}.db")
        db = Database(path)
        try:
            eng = Engine(FakeClient(), db, EventBus(), make_settings())
            ok = await eng._try_open_mystery()
            self.assertTrue(ok)

            persisted = float(db.get_kv("mystery_due_at", "0") or 0)
            self.assertGreater(persisted, time.time())

            eng2 = Engine(FakeClient(), db, EventBus(), make_settings())
            self.assertAlmostEqual(eng2._mystery_due_at, persisted, delta=1)
        finally:
            db.close(); os.remove(path)

    async def test_mystery_pack_interval_configurable(self):
        eng, client, db, path = make_engine(FakeClient(), mystery_interval_hours=3.0)
        try:
            before = time.time()
            ok = await eng._try_open_mystery()
            self.assertTrue(ok)
            after = time.time()

            expected_min = before + 3 * 3600
            expected_max = after + 3 * 3600 + 5
            self.assertGreaterEqual(eng._mystery_due_at, expected_min - 5)
            self.assertLessEqual(eng._mystery_due_at, expected_max)
        finally:
            db.close(); os.remove(path)


class RecycleQuotaCooldown(unittest.IsolatedAsyncioTestCase):
    async def test_quota_cooldown_persisted(self):
        import tempfile
        import uuid
        from app.db import Database
        from app.events import EventBus
        from app.engine import Engine

        path = os.path.join(tempfile.gettempdir(), f"_wtcg_{uuid.uuid4().hex}.db")
        db = Database(path)
        try:
            eng = Engine(
                FakeClient(dups=[dup("wiki-sr", "SR", ["a", "b", "c"])], quota_after=1),
                db, EventBus(), make_settings(keep_spares={"SR": 0}))
            n = await eng.recycle_pass()
            self.assertEqual(n, 1)

            persisted = float(db.get_kv("recycle_quota_until", "0") or 0)
            self.assertGreater(persisted, time.time())

            eng2 = Engine(FakeClient(), db, EventBus(), make_settings())
            self.assertGreater(eng2._recycle_quota_until, time.time())
        finally:
            db.close(); os.remove(path)


if __name__ == "__main__":
    unittest.main()
