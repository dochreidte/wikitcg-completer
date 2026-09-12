"""Tests for engine_market via FakeClient."""
import json
import os
import time
import unittest

from tests.fakes import FakeClient, make_engine, make_settings


def Settings_with_market(**engine_over):
    s = make_settings(**engine_over)
    s.raw["marketplace"]["enabled"] = True
    return s


class FulfilledListingHandling(unittest.IsolatedAsyncioTestCase):
    async def test_fulfilled_listing_without_wanted_card_not_credited(self):
        mine = [{"id": "L1", "status": "fulfilled", "wanted_card_id": None,
                 "wanted_series_id": "s", "offered_card_type": "wiki-O", "offered_series": "s"}]
        eng, client, db, path = make_engine(FakeClient(mine=mine), settings=Settings_with_market())
        try:
            await eng.marketplace_pass()
            self.assertFalse(db.owns_card(None))
        finally:
            db.close(); os.remove(path)

    async def test_fulfilled_listing_without_offered_card_not_decremented(self):
        mine = [{"id": "L1", "status": "fulfilled", "wanted_card_id": "wiki-W",
                 "wanted_series_id": "s", "offered_card_type": None, "offered_series": "s"}]
        eng, client, db, path = make_engine(FakeClient(mine=mine), settings=Settings_with_market())
        try:
            await eng.marketplace_pass()
            self.assertTrue(db.owns_card("wiki-W"))
        finally:
            db.close(); os.remove(path)

    async def test_fulfilled_by_alternative_status(self):
        mine = [{"id": "L1", "status": "active", "fulfilled_by": "player-123",
                 "wanted_card_id": "wiki-W", "wanted_series_id": "s",
                 "offered_card_type": "wiki-O", "offered_series": "s"}]
        eng, client, db, path = make_engine(FakeClient(mine=mine), settings=Settings_with_market())
        try:
            await eng.marketplace_pass()
            self.assertTrue(db.owns_card("wiki-W"))
            self.assertIn("L1", json.loads(db.get_kv("fulfilled_seen", "[]")))
        finally:
            db.close(); os.remove(path)


class ListingAgeCalculation(unittest.IsolatedAsyncioTestCase):
    async def test_listing_without_created_at_not_age_checked(self):
        mine = [{"id": "L1", "status": "active", "wanted_card_id": "wiki-not-needed",
                 "wanted_series_id": "s", "offered_card_type": "wiki-O", "created_at": None}]
        eng, client, db, path = make_engine(FakeClient(mine=mine), settings=Settings_with_market())
        try:
            db.upsert_series("s", "S", "#000", "#fff", 2)
            db.bump_inventory("s", "wiki-not-needed", "C")
            await eng.marketplace_pass()
            self.assertIn("L1", client.cancelled)
        finally:
            db.close(); os.remove(path)


class FulfillmentLogic(unittest.IsolatedAsyncioTestCase):
    async def test_fulfill_disabled_by_default(self):
        browse = [{"id": "L1", "offered_card_type": "wiki-gain", "wanted_card_id": "wiki-give"}]
        eng, client, db, path = make_engine(FakeClient(browse=browse),
                                            settings=Settings_with_market())
        try:
            await eng.marketplace_pass()
            self.assertEqual(len(client.fulfilled), 0)
        finally:
            db.close(); os.remove(path)


class MyListingsCache(unittest.IsolatedAsyncioTestCase):
    async def test_my_listings_cache_updated_each_pass(self):
        mine_initial = [{"id": "L1", "status": "active", "wanted_card_id": "wiki-W"}]
        client = FakeClient(mine=mine_initial)
        eng, client, db, path = make_engine(client, settings=Settings_with_market())
        try:
            await eng.marketplace_pass()
            self.assertEqual(len(eng._my_listings), 1)
            self.assertEqual(eng._my_listings[0]["id"], "L1")

            client._mine = [
                {"id": "L1", "status": "active", "wanted_card_id": "wiki-W"},
                {"id": "L2", "status": "active", "wanted_card_id": "wiki-W2"},
            ]
            await eng.marketplace_pass()
            self.assertEqual(len(eng._my_listings), 2)
        finally:
            db.close(); os.remove(path)


if __name__ == "__main__":
    unittest.main()
