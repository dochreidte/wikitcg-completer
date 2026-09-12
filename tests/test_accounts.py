import os
import tempfile
import unittest
import uuid

from app.db import Database


class Accounts(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.gettempdir(), f"_wtcg_{uuid.uuid4().hex}.db")
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()
        os.remove(self.path)

    def test_add_assigns_slug_id_and_db_path(self):
        acc = self.db.add_account(" My Acc! ", "space-cosmos")
        self.assertEqual(acc, {"id": "My_Acc", "name": "My Acc!", "series": "space-cosmos",
                               "db_path": "wikitcg_My_Acc.db"})
        self.assertEqual(self.db.get_account("My_Acc"), acc)

    def test_list_keeps_insertion_order(self):
        for name in ("b", "a", "c"):
            self.db.add_account(name)
        self.assertEqual([a["id"] for a in self.db.list_accounts()], ["b", "a", "c"])

    def test_colliding_slugs_get_suffix(self):
        self.assertEqual(self.db.add_account("a b")["id"], "a_b")
        self.assertEqual(self.db.add_account("a_b")["id"], "a_b-2")

    def test_invalid_input_rejected(self):
        self.db.add_account("main")
        for name, series in (("", ""), ("   ", ""), ("x" * 41, ""), ("ok", "../etc"), ("main", "")):
            with self.assertRaises(ValueError, msg=(name, series)):
                self.db.add_account(name, series)

    def test_update(self):
        acc = self.db.add_account("one", "")
        self.db.add_account("two", "")
        updated = self.db.update_account(acc["id"], name="uno", series="mystery")
        self.assertEqual(updated, dict(acc, name="uno", series="mystery"))
        self.assertIsNone(self.db.update_account("nope", name="x"))
        with self.assertRaises(ValueError):
            self.db.update_account(acc["id"], name="two")
        with self.assertRaises(ValueError):
            self.db.update_account(acc["id"], series="bad series")

    def test_delete(self):
        acc = self.db.add_account("one")
        self.assertTrue(self.db.delete_account(acc["id"]))
        self.assertFalse(self.db.delete_account(acc["id"]))
        self.assertEqual(self.db.list_accounts(), [])


if __name__ == "__main__":
    unittest.main()
