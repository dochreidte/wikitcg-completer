"""Tests des fonctions pures de stratégie. Lancer depuis la racine du projet :
    python -m unittest discover -s tests
"""
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
        # réserve SR = 1 -> surplus = 5 - 1 - 1 = 3
        self.assertEqual(strategy.plan_recycle(dups, {"SR": 1}), {"c1": 3})

    def test_no_surplus_when_within_reserve(self):
        dups = [{"card_id": "c1", "rarity": "LR", "quantity": 3}]
        # réserve LR = 2 -> surplus = 3 - 1 - 2 = 0
        self.assertEqual(strategy.plan_recycle(dups, {"LR": 2}), {})


class OfferRarities(unittest.TestCase):
    def test_lowest_eligible_first(self):
        # Pour viser une R, on peut offrir R ou SR ; on offre la plus faible d'abord.
        self.assertEqual(strategy.offer_rarities_for_target("R"), ["R", "SR"])

    def test_common_cannot_be_offered(self):
        # C ne peut jamais être offert -> n'apparaît dans aucune liste d'offre.
        for tgt in ["C", "UC", "R", "SR", "SSR", "UR", "LR"]:
            self.assertNotIn("C", strategy.offer_rarities_for_target(tgt))


class ExpectedPacksAndTrade(unittest.TestCase):
    def test_infinite_cost_when_nothing_missing(self):
        self.assertEqual(strategy.expected_packs_for_card("R", 0, 30), float("inf"))

    def test_cost_rises_as_fewer_missing(self):
        many = strategy.expected_packs_for_card("R", 20, 30)
        few = strategy.expected_packs_for_card("R", 1, 30)
        self.assertLess(many, few)  # moins de cibles -> plus cher

    def test_should_trade_true_when_costs_explode(self):
        # 1 seule LR manquante sur 3 -> coût espéré énorme -> bascule échange.
        self.assertTrue(strategy.should_trade_series({"LR": 1}, {"LR": 3},
                                                     trade_cost_packs_equiv=6.0))

    def test_should_trade_false_when_cheap_to_open(self):
        # Petit set presque entièrement manquant -> chaque pack est très rentable -> on OUVRE.
        # (Le coût dépend surtout du nombre de cartes DISTINCTES, pas du seul nb manquant.)
        self.assertFalse(strategy.should_trade_series({"C": 5}, {"C": 5},
                                                      trade_cost_packs_equiv=6.0))


if __name__ == "__main__":
    unittest.main()
