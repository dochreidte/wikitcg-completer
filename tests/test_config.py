import copy
import os
import unittest
from unittest import mock

from app import config


class DeepMerge(unittest.TestCase):
    def test_deep_merge_copies_base(self):
        base = {"x": {"y": [1, 2]}}
        result = config._deep_merge(base, {})
        result["x"]["y"].append(3)
        self.assertEqual(base["x"]["y"], [1, 2])

    def test_deep_merge_copies_over(self):
        over = {"x": {"y": [1, 2]}}
        result = config._deep_merge({}, over)
        result["x"]["y"].append(3)
        self.assertEqual(over["x"]["y"], [1, 2])

    def test_deep_merge_replaces_primitive_with_dict(self):
        self.assertEqual(config._deep_merge({"x": "s"}, {"x": {"y": 1}}), {"x": {"y": 1}})

    def test_deep_merge_nested_dict_recursion(self):
        result = config._deep_merge({"api": {"url": "http", "auth": "yes"}}, {"api": {"auth": "no"}})
        self.assertEqual(result["api"], {"url": "http", "auth": "no"})


class SettingsIsolation(unittest.TestCase):
    def test_two_settings_keep_spares_isolated(self):
        s1 = config.Settings(raw=copy.deepcopy(config._DEFAULTS))
        s2 = config.Settings(raw=copy.deepcopy(config._DEFAULTS))
        s1.engine["keep_spares"]["C"] = 999
        self.assertEqual(s2.engine["keep_spares"]["C"], 0)


class LoadSettings(unittest.TestCase):
    def test_takes_no_config_file(self):
        with self.assertRaises(TypeError):
            config.load_settings("config.toml")
        self.assertFalse(hasattr(config, "tomllib"))

    def test_returns_isolated_defaults(self):
        s1, s2 = config.load_settings(), config.load_settings()
        s1.engine["keep_spares"]["C"] = 99
        self.assertEqual(s2.engine["keep_spares"]["C"], 0)
        self.assertEqual(s1.paths["database"], "app.db")

    def test_ignores_legacy_env_vars(self):
        with mock.patch.dict(os.environ, {"WIKITCG_SESSION": "eyJ.x.y", "WIKITCG_CONFIG": "x.toml"}):
            s = config.load_settings()
        self.assertEqual(s.api["session_cookie"], "")
        self.assertFalse(s.has_session)

    def test_removed_settings_are_gone(self):
        s = config.load_settings()
        for section, key in (("engine", "auto_open"), ("engine", "on_empty"), ("marketplace", "dry_run")):
            self.assertNotIn(key, s.raw[section])


class SettingsProperties(unittest.TestCase):
    def test_has_session(self):
        self.assertTrue(config.Settings(raw={"api": {"session_cookie": "header.payload.sig"}}).has_session)
        self.assertFalse(config.Settings(raw={"api": {"session_cookie": ""}}).has_session)


if __name__ == "__main__":
    unittest.main()
