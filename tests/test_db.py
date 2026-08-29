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
        self.assertTrue(self.db.bump_inventory("s", "c1", "R"))    # new
        self.assertFalse(self.db.bump_inventory("s", "c1", "R"))   # duplicate
        self.assertTrue(self.db.owns_card("c1"))
        self.assertFalse(self.db.owns_card("unknown"))

    def test_decrement_floor_zero(self):
        self.db.bump_inventory("s", "c1", "R")
        self.db.decrement_inventory("s", "c1", by=5)
        self.assertFalse(self.db.owns_card("c1"))   # quantity dropped to 0

    def test_all_duplicates(self):
        self.db.bump_inventory("s", "c1", "R")
        self.db.bump_inventory("s", "c1", "R")      # quantity 2 -> duplicate
        self.db.bump_inventory("s", "c2", "R")      # quantity 1 -> not a duplicate
        dups = {d["card_id"] for d in self.db.all_duplicates()}
        self.assertEqual(dups, {"c1"})


class RecycleFailures(DbCase):
    def test_mark_skips_summary_clear(self):
        now = time.time()
        r1 = self.db.mark_recycle_failure("p1", "wiki-1", "SR", 1800, now)   # 1st failure -> +1800
        self.db.mark_recycle_failure("p2", "wiki-2", "LR", 1800, now)
        r2 = self.db.mark_recycle_failure("p1", "wiki-1", "SR", 1800, now)   # 2nd failure -> +3600
        self.assertAlmostEqual(r1, now + 1800, delta=2)
        self.assertAlmostEqual(r2, now + 3600, delta=2)                      # growing backoff
        skips = self.db.recycle_skips()
        self.assertEqual(set(skips), {"p1", "p2"})
        self.assertEqual(self.db.recycle_failures_summary()["total"], 2)
        self.db.clear_recycle_failure("p1")
        self.assertEqual(self.db.recycle_failures_summary()["total"], 1)
        self.assertNotIn("p1", self.db.recycle_skips())


class Progress(DbCase):
    def test_progress_view_owned_and_missing(self):
        self.db.upsert_series("s", "Series S", "#000", "#fff", 3)   # set_size 3
        self.db.bump_inventory("s", "c1", "R")
        self.db.bump_inventory("s", "c2", "R")
        p = next(x for x in self.db.progress_view() if x["series_id"] == "s")
        self.assertEqual(p["total"], 3)
        self.assertEqual(p["owned"], 2)
        self.assertEqual(p["missing"], 1)


if __name__ == "__main__":
    unittest.main()
