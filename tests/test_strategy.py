"""Strategy tests."""
import unittest

from app import strategy


class SelectTargetSeries(unittest.TestCase):
    def test_picks_series_with_most_missing(self):
        progress = [
            {"series_id": "a", "total": 200, "missing": 3, "pct": 98.5},
            {"series_id": "b", "total": 200, "missing": 40, "pct": 80.0},
        ]
        self.assertEqual(strategy.select_target_series(progress)["series_id"], "b")

    def test_tiebreak_prefers_lower_pct(self):
        progress = [
            {"series_id": "a", "total": 200, "missing": 10, "pct": 95.0},
            {"series_id": "b", "total": 200, "missing": 10, "pct": 80.0},
        ]
        self.assertEqual(strategy.select_target_series(progress)["series_id"], "b")

    def test_none_when_all_complete(self):
        progress = [{"series_id": "a", "total": 200, "missing": 0, "pct": 100.0}]
        self.assertIsNone(strategy.select_target_series(progress))

    def test_ignores_series_without_total(self):
        progress = [{"series_id": "a", "total": 0, "missing": 0, "pct": 0.0}]
        self.assertIsNone(strategy.select_target_series(progress))


class PlanRecycle(unittest.TestCase):
    def test_surplus_is_quantity_minus_one_minus_reserve(self):
        dups = [{"card_id": "c1", "rarity": "SR", "quantity": 5}]
        self.assertEqual(strategy.plan_recycle(dups, {"SR": 1}), {"c1": 3})

    def test_no_surplus_when_within_reserve(self):
        dups = [{"card_id": "c1", "rarity": "LR", "quantity": 3}]
        self.assertEqual(strategy.plan_recycle(dups, {"LR": 2}), {})


class OfferRarities(unittest.TestCase):
    def test_lowest_eligible_first(self):
        self.assertEqual(strategy.offer_rarities_for_target("R"), ["R", "SR"])

    def test_common_cannot_be_offered(self):
        for tgt in ["C", "UC", "R", "SR", "SSR", "UR", "LR"]:
            self.assertNotIn("C", strategy.offer_rarities_for_target(tgt))


if __name__ == "__main__":
    unittest.main()
