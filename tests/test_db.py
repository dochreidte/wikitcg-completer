"""SQLite layer tests (temporary file)."""
import os
import tempfile
import time
import unittest
import uuid

from app.db import Database


class DbCase(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.gettempdir(), f"_wtcg_{uuid.uuid4().hex}.db")
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()
        try:
            os.remove(self.path)
        except OSError:
            pass


class Inventory(DbCase):
    def test_bump_new_then_dup(self):
        self.assertTrue(self.db.bump_inventory("s", "c1", "R"))
        self.assertFalse(self.db.bump_inventory("s", "c1", "R"))
        self.assertTrue(self.db.owns_card("c1"))
        self.assertFalse(self.db.owns_card("unknown"))

    def test_decrement_floor_zero(self):
        self.db.bump_inventory("s", "c1", "R")
        self.db.decrement_inventory("s", "c1", by=5)
        self.assertFalse(self.db.owns_card("c1"))

    def test_all_duplicates(self):
        self.db.bump_inventory("s", "c1", "R")
        self.db.bump_inventory("s", "c1", "R")
        self.db.bump_inventory("s", "c2", "R")
        dups = {d["card_id"] for d in self.db.all_duplicates()}
        self.assertEqual(dups, {"c1"})


class RecycleFailures(DbCase):
    def test_mark_skips_summary_clear(self):
        now = time.time()
        r1 = self.db.mark_recycle_failure("p1", "wiki-1", "SR", 1800, now)
        self.db.mark_recycle_failure("p2", "wiki-2", "LR", 1800, now)
        r2 = self.db.mark_recycle_failure("p1", "wiki-1", "SR", 1800, now)
        self.assertAlmostEqual(r1, now + 1800, delta=2)
        self.assertAlmostEqual(r2, now + 3600, delta=2)
        skips = self.db.recycle_skips()
        self.assertEqual(set(skips), {"p1", "p2"})
        self.assertEqual(self.db.recycle_failures_summary()["total"], 2)
        self.db.clear_recycle_failure("p1")
        self.assertEqual(self.db.recycle_failures_summary()["total"], 1)
        self.assertNotIn("p1", self.db.recycle_skips())


class Progress(DbCase):
    def test_progress_view_owned_and_missing(self):
        self.db.upsert_series("s", "Series S", "#000", "#fff", 3)
        self.db.bump_inventory("s", "c1", "R")
        self.db.bump_inventory("s", "c2", "R")
        p = next(x for x in self.db.progress_view() if x["series_id"] == "s")
        self.assertEqual(p["total"], 3)
        self.assertEqual(p["owned"], 2)
        self.assertEqual(p["missing"], 1)

    def test_unknown_size_keeps_known_size(self):
        self.db.upsert_series("s", "Series S", "#000", "#fff", 200)
        self.db.upsert_series("s", "Renamed", "#111", "#eee", 0)
        p = next(x for x in self.db.progress_view() if x["series_id"] == "s")
        self.assertEqual((p["name"], p["total"]), ("Renamed", 200))

    def test_owned_count_excludes_zero_quantity(self):
        self.db.upsert_series("s", "Series S", "#000", "#fff", 3)
        self.db.bump_inventory("s", "c1", "R")
        self.db.bump_inventory("s", "c1", "R")
        self.db.decrement_inventory("s", "c1", by=2)
        self.assertEqual(self.db.owned_count("s"), 0)

    def test_missing_cards_includes_zero_quantity(self):
        self.db.upsert_series("s", "Series S", "#000", "#fff", 0)
        self.db.replace_catalog("s", [
            {"id": "c1", "rarity": "C", "cardNumber": 1, "title": "Card 1"},
            {"id": "c2", "rarity": "C", "cardNumber": 2, "title": "Card 2"},
        ])
        self.db.bump_inventory("s", "c1", "C")
        self.db.bump_inventory("s", "c1", "C")
        self.db.decrement_inventory("s", "c1", by=2)
        missing = self.db.missing_cards("s")
        missing_ids = {m["card_id"] for m in missing}
        self.assertIn("c1", missing_ids)
        self.assertIn("c2", missing_ids)

    def test_rarity_breakdown_excludes_zero_quantity(self):
        self.db.upsert_series("s", "Series S", "#000", "#fff", 0)
        self.db.replace_catalog("s", [
            {"id": "c1", "rarity": "SR", "cardNumber": 1, "title": "Card 1"},
            {"id": "c2", "rarity": "SR", "cardNumber": 2, "title": "Card 2"},
        ])
        self.db.bump_inventory("s", "c1", "SR")
        self.db.bump_inventory("s", "c1", "SR")
        self.db.decrement_inventory("s", "c1", by=2)
        breakdown = self.db.rarity_breakdown("s")
        sr = next((r for r in breakdown if r["rarity"] == "SR"), None)
        self.assertIsNotNone(sr)
        self.assertEqual(sr["owned"], 0, "Cards with quantity=0 should not be counted as owned")

    def test_progress_view_excludes_zero_quantity_owned(self):
        self.db.upsert_series("s", "Series S", "#000", "#fff", 4)
        self.db.bump_inventory("s", "c1", "C")
        self.db.bump_inventory("s", "c2", "C")
        self.db.decrement_inventory("s", "c2", by=1)
        self.db.replace_catalog("s", [
            {"id": "c1", "rarity": "C", "cardNumber": 1, "title": "Card 1"},
            {"id": "c2", "rarity": "C", "cardNumber": 2, "title": "Card 2"},
            {"id": "c3", "rarity": "C", "cardNumber": 3, "title": "Card 3"},
            {"id": "c4", "rarity": "C", "cardNumber": 4, "title": "Card 4"},
        ])
        p = next(x for x in self.db.progress_view() if x["series_id"] == "s")
        self.assertEqual(p["owned"], 1)
        self.assertEqual(p["total"], 4)
        self.assertEqual(p["missing"], 3)


if __name__ == "__main__":
    unittest.main()
