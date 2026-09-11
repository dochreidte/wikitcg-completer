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
        client = FakeClient(dups=[dup("wiki-1", "SR", ["p1", "p2", "p3", "p4"])], locked={"p1"})
        eng, client, db, path = make_engine(client)
        try:
            n = await eng.recycle_pass()
            self.assertEqual(n, 2)
            self.assertEqual(client.recycled, ["p2", "p3"])
            self.assertIn("p1", db.recycle_skips())
            self.assertEqual(db.recycle_failures_summary()["total"], 1)
        finally:
            db.close(); os.remove(path)

    async def test_skip_rarities_never_recycled(self):
        client = FakeClient(dups=[dup("wiki-lr", "LR", ["l1", "l2", "l3"]),
                                  dup("wiki-sr", "SR", ["s1", "s2", "s3"])])
        eng, client, db, path = make_engine(client, keep_spares={"LR": 0, "SR": 0},
                                            recycle_skip_rarities=["LR"])
        try:
            await eng.recycle_pass()
            self.assertFalse(any(p.startswith("l") for p in client.recycled))
            self.assertTrue(all(p.startswith("s") for p in client.recycled))
        finally:
            db.close(); os.remove(path)

    async def test_all_copies_locked_recycles_nothing(self):
        client = FakeClient(dups=[dup("wiki-1", "SR", ["p1", "p2", "p3"])], locked={"p1", "p2", "p3"})
        eng, client, db, path = make_engine(client)
        try:
            self.assertEqual(await eng.recycle_pass(), 0)
            self.assertEqual(client.recycled, [])
        finally:
            db.close(); os.remove(path)

    async def test_marketplace_reserve_keeps_a_spare(self):
        dups = [dup("wiki-1", "SR", ["p1", "p2"])]
        eng, client, db, path = make_engine(
            FakeClient(dups=dups), settings=make_settings(keep_spares={"SR": 0}))
        try:
            self.assertEqual(await eng.recycle_pass(), 1)
        finally:
            db.close(); os.remove(path)
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-1", "SR", ["p1", "p2"])]),
            settings=Settings_with_market(keep_spares={"SR": 0}))
        try:
            self.assertEqual(await eng.recycle_pass(), 0)
        finally:
            db.close(); os.remove(path)

    async def test_missing_mode_keeps_as_many_dups_as_missing(self):
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
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["a", "b", "c", "d"])]),
            settings=make_settings(recycle_reserve_mode="missing"))
        try:
            self.assertEqual(await eng.recycle_pass(), 3)
        finally:
            db.close(); os.remove(path)

    async def test_benign_400_ignored_not_retried(self):
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["a", "b", "c"])], notfound={"a"}),
            settings=make_settings(keep_spares={"SR": 0}))
        try:
            n = await eng.recycle_pass()
            self.assertEqual(n, 2)
            self.assertEqual(client.recycled, ["b", "c"])
            self.assertIn("a", eng._recycle_done)
            self.assertEqual(db.recycle_failures_summary()["total"], 0)
        finally:
            db.close(); os.remove(path)

    async def test_recycle_priority_rare_first(self):
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["s1", "s2", "s3"]),
                             dup("wiki-lr", "LR", ["l1", "l2", "l3"])]),
            settings=make_settings(keep_spares={"SR": 0, "LR": 0}, recycle_priority="rare_first"))
        try:
            await eng.recycle_pass()
            self.assertEqual(client.recycled, ["l1", "l2", "s1", "s2"])
        finally:
            db.close(); os.remove(path)

    async def test_recycle_priority_common_first_default(self):
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["s1", "s2", "s3"]),
                             dup("wiki-lr", "LR", ["l1", "l2", "l3"])]),
            settings=make_settings(keep_spares={"SR": 0, "LR": 0}))
        try:
            await eng.recycle_pass()
            self.assertEqual(client.recycled, ["s1", "s2", "l1", "l2"])
        finally:
            db.close(); os.remove(path)

    async def test_quota_429_stops_pass_and_arms_cooldown(self):
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["a", "b", "c", "d"])], quota_after=2),
            settings=make_settings(keep_spares={"SR": 0}))
        try:
            n = await eng.recycle_pass()
            self.assertEqual(n, 2)
            self.assertEqual(client.recycled, ["a", "b"])
            self.assertGreater(eng._recycle_quota_until, time.time())
            self.assertEqual(db.recycle_failures_summary()["total"], 0)
        finally:
            db.close(); os.remove(path)

    async def test_recycle_skipped_during_quota_cooldown(self):
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
        client = FakeClient(dups=[dup("wiki-1", "SR", ["p1", "p2", "p3"])])
        eng, client, db, path = make_engine(client)
        try:
            eng.resources = {"total_available": 0, "ink": 0}
            self.assertFalse(eng._wake.is_set())
            await eng.recycle_pass()
            self.assertTrue(eng._wake.is_set())
        finally:
            db.close(); os.remove(path)

    async def test_target_ink_stops_when_reached(self):
        dups = [dup("wiki-1", "SR", ["p1", "p2", "p3", "p4", "p5"])]
        eng, client, db, path = make_engine(
            FakeClient(status={"ink": 0}, dups=dups), settings=make_settings(keep_spares={"SR": 0}))
        try:
            n = await eng.recycle_pass(target_ink=80)
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

    async def test_announces_series_completion_once(self):
        client = FakeClient(open_result={
            "cards": [{"id": "s-1", "rarity": "C", "article": {"title": "B"}}],
            "series": {"name": "S"}, "totalAvailable": 4})
        eng, client, db, path = make_engine(client)
        try:
            db.upsert_series("s", "S", "#000", "#fff", 2)
            db.bump_inventory("s", "s-0", "C")
            await eng.open_one("s")
            await eng.open_one("s")
            self.assertEqual(len([a for a in db.recent_actions() if a["type"] == "complete"]), 1)
            self.assertEqual(db.progress_view()[0]["total"], 2)
        finally:
            db.close(); os.remove(path)


def series_meta(sid, card_count, name=None):
    """One GET /api/series row."""
    return {"id": sid, "name": name or sid.title(), "primaryColor": "#123",
            "accentColor": "#abc", "cardCount": card_count}


def catalog(sid, n, rarity="C"):
    """GET /api/series/{sid}/cards card list."""
    return [{"id": f"{sid}-{i}", "rarity": rarity, "cardNumber": i, "title": f"{sid} {i}"}
            for i in range(n)]


def announced(db, type_="series"):
    return [a["series_id"] for a in db.recent_actions() if a["type"] == type_]


class SeriesDetection(unittest.IsolatedAsyncioTestCase):
    async def test_full_sync_detects_new_series_and_lists_missing(self):
        client = FakeClient(series=[series_meta("old", 2), series_meta("ocean", 3, "Ocean")],
                            catalogs={"old": catalog("old", 2), "ocean": catalog("ocean", 3, "LR")},
                            collection=[{"series_id": "old", "owned": 2, "total_pulls": 2}])
        eng, client, db, path = make_engine(client)
        try:
            db.upsert_series("old", "Old", "#000", "#fff", 2)
            await eng.full_sync()
            ocean = next(p for p in db.progress_view() if p["series_id"] == "ocean")
            self.assertEqual((ocean["name"], ocean["total"], ocean["missing"]), ("Ocean", 3, 3))
            self.assertEqual(len(db.missing_cards("ocean")), 3)
            self.assertEqual(announced(db), ["ocean"])
        finally:
            db.close(); os.remove(path)

    async def test_first_sync_announces_nothing(self):
        client = FakeClient(series=[series_meta("a", 2)], catalogs={"a": catalog("a", 2)})
        eng, client, db, path = make_engine(client)
        try:
            await eng.full_sync()
            self.assertEqual(db.catalog_size("a"), 2)
            self.assertEqual(announced(db), [])
        finally:
            db.close(); os.remove(path)

    async def test_periodic_check_loads_new_series_inventory(self):
        client = FakeClient(series=[series_meta("a", 2), series_meta("b", 3)],
                            catalogs={"b": catalog("b", 3)},
                            details={"b": [{"card_id": "b-0", "rarity": "C", "quantity": 1}]})
        eng, client, db, path = make_engine(client)
        try:
            db.upsert_series("a", "A", "#000", "#fff", 2)
            self.assertEqual(await eng.check_series(), ["b"])
            self.assertEqual(eng._openable, {"a", "b"})
            b = next(p for p in db.progress_view() if p["series_id"] == "b")
            self.assertEqual((b["owned"], b["missing"]), (1, 2))
            self.assertEqual(announced(db), ["b"])
        finally:
            db.close(); os.remove(path)

    async def test_resized_series_reloads_catalog(self):
        client = FakeClient(series=[series_meta("a", 3)], catalogs={"a": catalog("a", 3)})
        eng, client, db, path = make_engine(client)
        try:
            db.upsert_series("a", "A", "#000", "#fff", 2)
            db.replace_catalog("a", catalog("a", 2))
            await eng.refresh_series()
            self.assertEqual(db.catalog_size("a"), 3)
            self.assertEqual(db.progress_view()[0]["total"], 3)
            self.assertEqual(announced(db), ["a"])
        finally:
            db.close(); os.remove(path)


def prog(sid, total, owned):
    """One Database.progress_view() row."""
    return {"series_id": sid, "name": sid, "total": total, "owned": owned,
            "missing": total - owned, "pct": round(100 * owned / total, 1) if total else 0.0}


class PickTarget(unittest.TestCase):
    def test_incomplete_series_before_pinned_series(self):
        eng, client, db, path = make_engine(only_series="farm")
        try:
            progress = [prog("farm", 200, 200), prog("ocean", 200, 6)]
            self.assertEqual(eng._pick_target(progress)["series_id"], "ocean")
        finally:
            db.close(); os.remove(path)

    def test_pinned_series_once_everything_complete(self):
        eng, client, db, path = make_engine(only_series="farm")
        try:
            progress = [prog("farm", 200, 200), prog("ocean", 200, 200)]
            self.assertEqual(eng._pick_target(progress)["series_id"], "farm")
        finally:
            db.close(); os.remove(path)

    def test_strict_mode_always_pinned(self):
        eng, client, db, path = make_engine(only_series="farm", only_series_mode="strict")
        try:
            progress = [prog("farm", 200, 200), prog("ocean", 200, 6)]
            self.assertEqual(eng._pick_target(progress)["series_id"], "farm")
        finally:
            db.close(); os.remove(path)

    def test_all_complete_keeps_opening_best_paying_series(self):
        eng, client, db, path = make_engine()
        try:
            for sid, rarity in (("cheap", "C"), ("rich", "LR"), ("mystery", "LR")):
                db.log_pull(sid, f"{sid}-1", rarity, False)
            progress = [prog("cheap", 200, 200), prog("mystery", 50, 50), prog("rich", 200, 200)]
            self.assertEqual(eng._pick_target(progress)["series_id"], "rich")
            self.assertEqual(eng._pick_target([prog("new", 200, 200)])["series_id"], "new")
        finally:
            db.close(); os.remove(path)

    def test_never_targets_non_openable_series(self):
        eng, client, db, path = make_engine()
        try:
            progress = [prog("mystery", 60, 50), prog("retired", 100, 10)]
            self.assertEqual(eng._pick_target(progress)["series_id"], "retired")
            eng._openable = {"ocean"}
            self.assertIsNone(eng._pick_target(progress))
        finally:
            db.close(); os.remove(path)


class BuyPacks(unittest.IsolatedAsyncioTestCase):
    async def test_respects_reserve(self):
        eng, client, db, path = make_engine(FakeClient(), settings=make_settings(min_ink_reserve=200))
        try:
            eng.resources = {"ink": 500}
            self.assertFalse(await eng._try_buy_packs())
            eng.resources = {"ink": 650}
            self.assertTrue(await eng._try_buy_packs())
        finally:
            db.close(); os.remove(path)


class WaitLogic(unittest.IsolatedAsyncioTestCase):
    async def test_race_condition_clear_before_wait(self):
        client = FakeClient(status={"totalAvailable": 0})
        eng, client, db, path = make_engine(client)
        try:
            eng.resources = {"total_available": 0, "ink": 100}
            eng._wake.set()
            eng._wake.clear()
            self.assertEqual(eng.resources.get("total_available", 0), 0)
        finally:
            db.close(); os.remove(path)


class Mystery(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_sets_retry(self):
        client = FakeClient(open_result={"error": "mystery_cooldown"})
        eng, client, db, path = make_engine(client)
        try:
            ok = await eng._try_open_mystery()
            self.assertFalse(ok)
            self.assertGreater(eng._mystery_due_at, time.time())
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
        mine = [{"id": "L1", "status": "fulfilled", "wanted_card_id": "wiki-W",
                 "wanted_series_id": "s", "offered_card_type": "wiki-O", "offered_series": "s"}]
        eng, client, db, path = make_engine(FakeClient(mine=mine), settings=Settings_with_market())
        try:
            await eng.marketplace_pass()
            self.assertTrue(db.owns_card("wiki-W"))
            self.assertIn("L1", json.loads(db.get_kv("fulfilled_seen", "[]")))
            await eng.marketplace_pass()
        finally:
            db.close(); os.remove(path)

    async def test_old_listing_cancelled(self):
        old_ms = (time.time() - 2 * 86400) * 1000
        mine = [{"id": "L1", "status": "active", "wanted_card_id": "wiki-W",
                 "wanted_series_id": "s", "offered_card_type": "wiki-O", "created_at": old_ms}]
        eng, client, db, path = make_engine(FakeClient(mine=mine), settings=Settings_with_market())
        try:
            await eng.marketplace_pass()
            self.assertIn("L1", client.cancelled)
        finally:
            db.close(); os.remove(path)


def Settings_with_market(**engine_over):
    s = make_settings(**engine_over)
    s.raw["marketplace"]["enabled"] = True
    return s


if __name__ == "__main__":
    unittest.main()
