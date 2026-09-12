import os
import unittest
from unittest import mock

import keyring
from keyring.backends import fail

from app import vault
from tests.fakes import MemoryKeyring

SECRET = "s3cr3t-value"


class Vault(unittest.TestCase):
    def setUp(self):
        self.prev = keyring.get_keyring()
        self.ring = MemoryKeyring()
        keyring.set_keyring(self.ring)

    def tearDown(self):
        keyring.set_keyring(self.prev)

    def test_roundtrip(self):
        vault.store("acc1", "session", SECRET)
        self.assertEqual(vault.get("acc1", "session"), SECRET)
        self.assertEqual(vault.get("acc1", "extra"), "")
        self.assertEqual(vault.get("other", "session"), "")

    def test_long_values_are_chunked_under_os_limit(self):
        value = "x" * 5000
        vault.store("acc1", "session", value)
        self.assertEqual(vault.get("acc1", "session"), value)
        self.assertTrue(all(len(v) <= 1000 for v in self.ring.data.values()))

    def test_overwrite_with_shorter_value_drops_old_chunks(self):
        vault.store("acc1", "session", "x" * 3000)
        vault.store("acc1", "session", "short")
        self.assertEqual(vault.get("acc1", "session"), "short")
        self.assertEqual(len(self.ring.data), 2)

    def test_empty_value_deletes(self):
        vault.store("acc1", "session", SECRET)
        vault.store("acc1", "session", "")
        self.assertEqual(self.ring.data, {})

    def test_missing_chunk_reads_as_empty(self):
        vault.store("acc1", "session", "x" * 2500)
        del self.ring.data[(vault.SERVICE, "acc1:session:1")]
        self.assertEqual(vault.get("acc1", "session"), "")

    def test_delete_account_removes_everything(self):
        vault.store("acc1", "session", "x" * 2500)
        vault.store("acc1", "extra", "cf=1")
        vault.store("acc2", "session", "keep")
        vault.delete("acc1")
        self.assertEqual(vault.get("acc1", "session"), "")
        self.assertEqual(vault.get("acc1", "extra"), "")
        self.assertEqual(vault.get("acc2", "session"), "keep")

    def test_env_var_wins(self):
        vault.store("my-acc", "session", "stored")
        with mock.patch.dict(os.environ, {"WIKITCG_SESSION_MY_ACC": "from-env"}):
            self.assertEqual(vault.get("my-acc", "session"), "from-env")
        self.assertEqual(vault.env_name("my-acc", "extra"), "WIKITCG_EXTRA_COOKIES_MY_ACC")

    def test_available(self):
        self.assertTrue(vault.available())
        keyring.set_keyring(fail.Keyring())
        self.assertFalse(vault.available())

    def test_without_backend_get_is_empty_and_store_raises(self):
        keyring.set_keyring(fail.Keyring())
        self.assertEqual(vault.get("acc1", "session"), "")
        with self.assertRaises(vault.VaultError) as ctx:
            vault.store("acc1", "session", SECRET)
        self.assertIn("WIKITCG_SESSION_ACC1", str(ctx.exception))
        self.assertNotIn(SECRET, str(ctx.exception))
        vault.delete("acc1")

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            vault.store("acc1", "password", "x")


if __name__ == "__main__":
    unittest.main()
