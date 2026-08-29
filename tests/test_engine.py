"""Engine integration tests via FakeClient (no network)."""
import json
import os
import time
import unittest

from tests.fakes import FakeClient, make_engine, make_settings


def dup(card, rarity, pulls, series="s"):
    return {"card_id": card, "series_id": series, "rarity": rarity,
            "copies": len(pulls), "pull_ids": list(pulls)}


class RecyclePass(unittest.IsolatedAsyncioTestCase):
    async def test_skips_500_and_tries_other_copies(self):
        # SR copies=4, SR reserve=1 -> surplus 2. p1 locked (500): we must recycle
        # 2 OTHER copies of the same type (p2, p3) instead of wasting the quota.
        client = FakeClient(dups=[dup("wiki-1", "SR", ["p1", "p2", "p3", "p4"])], locked={"p1"})
        eng, client, db, path = make_engine(client)
        try:
            n = await eng.recycle_pass()
            self.assertEqual(n, 2)                              # full surplus recycled
            self.assertEqual(client.recycled, ["p2", "p3"])     # p1 skipped, p2+p3 recycled
            self.assertIn("p1", db.recycle_skips())             # p1 remembered in the db (cooldown)
            self.assertEqual(db.recycle_failures_summary()["total"], 1)
        finally:
            db.close(); os.remove(path)

    async def test_skip_rarities_never_recycled(self):
        # recycle_skip_rarities=["LR"]: LR are never recycled, the others are.
        client = FakeClient(dups=[dup("wiki-lr", "LR", ["l1", "l2", "l3"]),
                                  dup("wiki-sr", "SR", ["s1", "s2", "s3"])])
        eng, client, db, path = make_engine(client, keep_spares={"LR": 0, "SR": 0},
                                            recycle_skip_rarities=["LR"])
        try:
            await eng.recycle_pass()
            self.assertFalse(any(p.startswith("l") for p in client.recycled))  # no LR
            self.assertTrue(all(p.startswith("s") for p in client.recycled))   # only SR
        finally:
            db.close(); os.remove(path)

    async def test_all_copies_locked_recycles_nothing(self):
        # If ALL copies of a type are locked -> 0 recycled, all in cooldown.
        client = FakeClient(dups=[dup("wiki-1", "SR", ["p1", "p2", "p3"])], locked={"p1", "p2", "p3"})
        eng, client, db, path = make_engine(client)
        try:
            self.assertEqual(await eng.recycle_pass(), 0)
            self.assertEqual(client.recycled, [])
        finally:
            db.close(); os.remove(path)

    async def test_marketplace_reserve_keeps_a_spare(self):
        # SR copies=2, keep_spares SR=0. Marketplace OFF -> recycle 1; ON -> keep 1 (recycle 0).
        dups = [dup("wiki-1", "SR", ["p1", "p2"])]
        eng, client, db, path = make_engine(
            FakeClient(dups=dups), settings=make_settings(keep_spares={"SR": 0}))
        try:
            self.assertEqual(await eng.recycle_pass(), 1)      # marketplace disabled
        finally:
            db.close(); os.remove(path)
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-1", "SR", ["p1", "p2"])]),
            settings=Settings_with_market(keep_spares={"SR": 0}))
        try:
            self.assertEqual(await eng.recycle_pass(), 0)      # marketplace reserve -> nothing
        finally:
            db.close(); os.remove(path)

    async def test_missing_mode_keeps_as_many_dups_as_missing(self):
        # "missing" mode: keep, PER RARITY, as many duplicates as there are missing cards.
        # 16 LR copies -> 15 duplicates; the catalog has 3 LR never owned (3 missing)
        # -> budget = 15 - 3 = 12 copies recycled.
        pulls = [f"p{i}" for i in range(16)]
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-lr", "LR", pulls)]),
            settings=make_settings(recycle_reserve_mode="missing"))
        try:
            db.replace_catalog("s", [{"id": f"cat-lr-{i}", "rarity": "LR",
                                      "cardNumber": i, "title": f"L{i}"} for i in range(3)])
            n = await eng.recycle_pass()
            self.assertEqual(n, 12)
            self.assertEqual(len(client.recycled), 12)
        finally:
            db.close(); os.remove(path)

    async def test_missing_mode_no_missing_recycles_all_dups(self):
        # No missing card of the rarity -> budget = all duplicates (copies - 1).
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["a", "b", "c", "d"])]),
            settings=make_settings(recycle_reserve_mode="missing"))
        try:
            self.assertEqual(await eng.recycle_pass(), 3)   # 4 copies -> 3 duplicates, 0 missing
        finally:
            db.close(); os.remove(path)

    async def test_benign_400_ignored_not_retried(self):
        # 400 cards_not_found (copy already recycled, stale /duplicates): we ignore it WITHOUT
        # counting it as an error to retry, and we mark it "done" (never resubmitted).
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["a", "b", "c"])], notfound={"a"}),
            settings=make_settings(keep_spares={"SR": 0}))
        try:
            n = await eng.recycle_pass()
            self.assertEqual(n, 2)                                  # b, c recycled; a (400) ignored
            self.assertEqual(client.recycled, ["b", "c"])
            self.assertIn("a", eng._recycle_done)                   # will not be retried
            self.assertEqual(db.recycle_failures_summary()["total"], 0)  # NOT counted as a 500 failure
        finally:
            db.close(); os.remove(path)

    async def test_recycle_priority_rare_first(self):
        # "rare_first" (pure farm): recycle the rarest first (LR before SR) to extract the most
        # ink under the daily quota. SR & LR each have 2 recyclable duplicates.
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["s1", "s2", "s3"]),
                             dup("wiki-lr", "LR", ["l1", "l2", "l3"])]),
            settings=make_settings(keep_spares={"SR": 0, "LR": 0}, recycle_priority="rare_first"))
        try:
            await eng.recycle_pass()
            self.assertEqual(client.recycled, ["l1", "l2", "s1", "s2"])   # LR first
        finally:
            db.close(); os.remove(path)

    async def test_recycle_priority_common_first_default(self):
        # Default "common_first": sacrifice the least rare first (SR before LR) — preserves LR.
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["s1", "s2", "s3"]),
                             dup("wiki-lr", "LR", ["l1", "l2", "l3"])]),
            settings=make_settings(keep_spares={"SR": 0, "LR": 0}))
        try:
            await eng.recycle_pass()
            self.assertEqual(client.recycled, ["s1", "s2", "l1", "l2"])   # SR first
        finally:
            db.close(); os.remove(path)

    async def test_quota_429_stops_pass_and_arms_cooldown(self):
        # A 429 on /recycle = DAILY quota reached: stop the pass right away and arm a cooldown
        # (instead of hammering); the card is NOT counted as a 500 error.
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["a", "b", "c", "d"])], quota_after=2),
            settings=make_settings(keep_spares={"SR": 0}))
        try:
            n = await eng.recycle_pass()
            self.assertEqual(n, 2)                       # 2 recycled, then 429 -> stop
            self.assertEqual(client.recycled, ["a", "b"])
            self.assertGreater(eng._recycle_quota_until, time.time())     # cooldown armed
            self.assertEqual(db.recycle_failures_summary()["total"], 0)   # not marked 500
        finally:
            db.close(); os.remove(path)

    async def test_recycle_skipped_during_quota_cooldown(self):
        # During the quota cooldown, the pass does NOTHING (doesn't even call /recycle).
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["a", "b"])]),
            settings=make_settings(keep_spares={"SR": 0}))
        try:
            eng._recycle_quota_until = time.time() + 3600
            self.assertEqual(await eng.recycle_pass(), 0)
            self.assertEqual(client.recycled, [])
        finally:
            db.close(); os.remove(path)

    async def test_recycle_wakes_opener_when_packs_empty(self):
        # Ink gained + no more packs -> wake the opening loop to buy.
        client = FakeClient(dups=[dup("wiki-1", "SR", ["p1", "p2", "p3"])])
        eng, client, db, path = make_engine(client)
        try:
            eng.resources = {"total_available": 0, "ink": 0}
            self.assertFalse(eng._wake.is_set())
            await eng.recycle_pass()
            self.assertTrue(eng._wake.is_set())   # wake-up set
        finally:
            db.close(); os.remove(path)

    async def test_target_ink_stops_when_reached(self):
        dups = [dup("wiki-1", "SR", ["p1", "p2", "p3", "p4", "p5"])]
        eng, client, db, path = make_engine(
            FakeClient(status={"ink": 0}, dups=dups), settings=make_settings(keep_spares={"SR": 0}))
        try:
            n = await eng.recycle_pass(target_ink=80)          # 40 ink/card -> 2 cards
            self.assertEqual(n, 2)
        finally:
            db.close(); os.remove(path)


class OpenOne(unittest.IsolatedAsyncioTestCase):
    async def test_cancels_listing_when_wanted_pulled(self):
        client = FakeClient(open_result={
            "cards": [{"id": "wiki-want", "rarity": "R", "article": {"title": "W"}}],
            "series": {"name": "S"}, "xpEarned": 5, "totalAvailable": 4})
        eng, client, db, path = make_engine(client)
        try:
            eng._my_listings = [{"id": "L1", "wanted_card_id": "wiki-want"}]
            await eng.open_one("s")
            self.assertIn("L1", client.cancelled)
            self.assertEqual(eng._my_listings, [])
        finally:
            db.close(); os.remove(path)

    async def test_reads_packs_from_response(self):
        client = FakeClient(open_result={
            "cards": [{"id": "wiki-a", "rarity": "C", "article": {"title": "A"}}],
            "series": {"name": "S"}, "totalAvailable": 3})
        eng, client, db, path = make_engine(client)
        try:
            await eng.open_one("s")
            self.assertEqual(eng.resources["total_available"], 3)
        finally:
            db.close(); os.remove(path)


class BuyPacks(unittest.IsolatedAsyncioTestCase):
    async def test_respects_reserve(self):
        eng, client, db, path = make_engine(FakeClient(), settings=make_settings(min_ink_reserve=200))
        try:
            eng.resources = {"ink": 500}
            self.assertFalse(await eng._try_buy_packs())       # 500 < 400 + 200
            eng.resources = {"ink": 650}
            self.assertTrue(await eng._try_buy_packs())        # 650 >= 600
        finally:
            db.close(); os.remove(path)


class Mystery(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_sets_retry(self):
        client = FakeClient(open_result={"error": "mystery_cooldown"})
        eng, client, db, path = make_engine(client)
        try:
            ok = await eng._try_open_mystery()
            self.assertFalse(ok)
            self.assertGreater(eng._mystery_due_at, time.time())   # retry later
        finally:
            db.close(); os.remove(path)

    async def test_success_sets_6h(self):
        eng, client, db, path = make_engine(FakeClient())
        try:
            ok = await eng._try_open_mystery()
            self.assertTrue(ok)
            self.assertGreater(eng._mystery_due_at, time.time() + 5 * 3600)
        finally:
            db.close(); os.remove(path)


class Marketplace(unittest.IsolatedAsyncioTestCase):
    async def test_fulfilled_credited_and_not_reoffered(self):
        # Listing fulfilled -> credit the wanted card (no longer re-offered); handled once.
        mine = [{"id": "L1", "status": "fulfilled", "wanted_card_id": "wiki-W",
                 "wanted_series_id": "s", "offered_card_type": "wiki-O", "offered_series": "s"}]
        eng, client, db, path = make_engine(FakeClient(mine=mine), settings=Settings_with_market())
        try:
            await eng.marketplace_pass()
            self.assertTrue(db.owns_card("wiki-W"))                     # credited
            self.assertIn("L1", json.loads(db.get_kv("fulfilled_seen", "[]")))
            await eng.marketplace_pass()                                # 2nd pass: no re-credit
        finally:
            db.close(); os.remove(path)

    async def test_old_listing_cancelled(self):
        old_ms = (time.time() - 2 * 86400) * 1000   # created 2 days ago
        mine = [{"id": "L1", "status": "active", "wanted_card_id": "wiki-W",
                 "wanted_series_id": "s", "offered_card_type": "wiki-O", "created_at": old_ms}]
        eng, client, db, path = make_engine(FakeClient(mine=mine), settings=Settings_with_market())
        try:
            await eng.marketplace_pass()
            self.assertIn("L1", client.cancelled)                      # cancelled because > 1 day
        finally:
            db.close(); os.remove(path)


def Settings_with_market(**engine_over):
    s = make_settings(**engine_over)
    s.raw["marketplace"]["enabled"] = True
    return s


if __name__ == "__main__":
    unittest.main()
