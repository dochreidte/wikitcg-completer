"""Tests des fonctions pures de marketplace."""
import unittest

from app import marketplace


class PlanListings(unittest.TestCase):
    def _scenario(self):
        progress = [
            {"series_id": "A", "missing": 1},   # proche de la complétion
            {"series_id": "B", "missing": 5},
        ]
        missing_by_series = {
            "A": [{"card_id": "a1", "rarity": "R"}],
            "B": [{"card_id": "b1", "rarity": "SR"}],
        }
        dups = [
            {"card_id": "dR", "rarity": "R", "series_id": "C", "quantity": 2},   # 1 dispo
            {"card_id": "dSR", "rarity": "SR", "series_id": "C", "quantity": 2},
        ]
        return progress, missing_by_series, dups

    def test_offers_lowest_eligible_and_prioritises_near_completion(self):
        progress, missing_by_series, dups = self._scenario()
        plans = marketplace.plan_listings(missing_by_series, dups, {}, set(), 1, progress, True)
        self.assertEqual(len(plans), 1)
        pl = plans[0]
        self.assertEqual(pl["wanted_card"], "a1")        # série A (1 manquante) d'abord
        self.assertEqual(pl["offered_type"], "dR")       # rareté R = la plus faible éligible

    def test_skips_already_wanted(self):
        progress, missing_by_series, dups = self._scenario()
        plans = marketplace.plan_listings(missing_by_series, dups, {}, {"a1"}, 5, progress, True)
        wanted = {p["wanted_card"] for p in plans}
        self.assertNotIn("a1", wanted)

    def test_respects_n_needed(self):
        progress, missing_by_series, dups = self._scenario()
        plans = marketplace.plan_listings(missing_by_series, dups, {}, set(), 0, progress, True)
        self.assertEqual(plans, [])

    def test_no_offer_when_no_spare_available(self):
        progress, missing_by_series, _ = self._scenario()
        plans = marketplace.plan_listings(missing_by_series, [], {}, set(), 2, progress, True)
        self.assertEqual(plans, [])

    def test_never_offers_a_single_copy(self):
        # RÈGLE 1 : une carte possédée en 1 seul exemplaire (quantity=1) ne doit JAMAIS être offerte.
        progress = [{"series_id": "A", "missing": 1}]
        missing_by_series = {"A": [{"card_id": "a1", "rarity": "R"}]}
        single = [{"card_id": "dR", "rarity": "R", "series_id": "C", "quantity": 1}]   # 1 exemplaire
        self.assertEqual(marketplace.plan_listings(missing_by_series, single, {}, set(), 1, progress), [])
        # avec 2 exemplaires, l'offre devient possible (on garde 1).
        dbl = [{"card_id": "dR", "rarity": "R", "series_id": "C", "quantity": 2}]
        self.assertEqual(len(marketplace.plan_listings(missing_by_series, dbl, {}, set(), 1, progress)), 1)

    def test_does_not_overcommit_already_listed_copies(self):
        # 2 exemplaires mais 1 déjà engagé dans une annonce active -> plus rien à offrir.
        progress = [{"series_id": "A", "missing": 1}]
        missing_by_series = {"A": [{"card_id": "a1", "rarity": "R"}]}
        dbl = [{"card_id": "dR", "rarity": "R", "series_id": "C", "quantity": 2}]
        plans = marketplace.plan_listings(missing_by_series, dbl, {"dR": 1}, set(), 1, progress)
        self.assertEqual(plans, [])


class PlanFulfillments(unittest.TestCase):
    def test_matches_missing_with_spare(self):
        listings = [{"id": "L1", "offered_card_type": "x", "wanted_card_id": "y"}]
        deals = marketplace.plan_fulfillments(listings, {"x"}, {"y": 1}, 3)
        self.assertEqual(deals, [{"listing_id": "L1", "gain_card": "x", "give_card": "y"}])

    def test_skip_when_not_missing(self):
        listings = [{"id": "L1", "offered_card_type": "x", "wanted_card_id": "y"}]
        self.assertEqual(marketplace.plan_fulfillments(listings, set(), {"y": 1}, 3), [])

    def test_skip_when_no_spare_to_give(self):
        listings = [{"id": "L1", "offered_card_type": "x", "wanted_card_id": "y"}]
        self.assertEqual(marketplace.plan_fulfillments(listings, {"x"}, {"y": 0}, 3), [])

    def test_respects_max_and_no_double_spend(self):
        listings = [
            {"id": "L1", "offered_card_type": "x1", "wanted_card_id": "y"},
            {"id": "L2", "offered_card_type": "x2", "wanted_card_id": "y"},
        ]
        # un seul exemplaire de "y" en réserve -> un seul deal
        deals = marketplace.plan_fulfillments(listings, {"x1", "x2"}, {"y": 1}, 5)
        self.assertEqual(len(deals), 1)

    def test_requires_both_conditions(self):
        # RÈGLE 2 : honorer SEULEMENT si (a) carte demandée en double ET (b) carte offerte non possédée.
        listing = [{"id": "L1", "offered_card_type": "GAIN", "wanted_card_id": "GIVE"}]
        # (a) ET (b) vraies -> accepté
        self.assertEqual(len(marketplace.plan_fulfillments(listing, {"GAIN"}, {"GIVE": 1}, 5)), 1)
        # (a) faux : je n'ai pas "GIVE" en double -> refusé
        self.assertEqual(marketplace.plan_fulfillments(listing, {"GAIN"}, {"GIVE": 0}, 5), [])
        self.assertEqual(marketplace.plan_fulfillments(listing, {"GAIN"}, {}, 5), [])
        # (b) faux : je possède déjà "GAIN" (absent de mes manquantes) -> refusé
        self.assertEqual(marketplace.plan_fulfillments(listing, set(), {"GIVE": 1}, 5), [])


if __name__ == "__main__":
    unittest.main()
