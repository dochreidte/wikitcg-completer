"""Tests d'intégration du moteur via FakeClient (sans réseau)."""
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
        # SR copies=4, réserve SR=1 -> surplus 2. p1 verrouillé (500) : on doit recycler
        # 2 AUTRES exemplaires du même type (p2, p3) au lieu de gâcher le quota.
        client = FakeClient(dups=[dup("wiki-1", "SR", ["p1", "p2", "p3", "p4"])], locked={"p1"})
        eng, client, db, path = make_engine(client)
        try:
            n = await eng.recycle_pass()
            self.assertEqual(n, 2)                              # surplus complet recyclé
            self.assertEqual(client.recycled, ["p2", "p3"])     # p1 sauté, p2+p3 recyclés
            self.assertIn("p1", db.recycle_skips())             # p1 mémorisé en base (cooldown)
            self.assertEqual(db.recycle_failures_summary()["total"], 1)
        finally:
            db.close(); os.remove(path)

    async def test_skip_rarities_never_recycled(self):
        # recycle_skip_rarities=["LR"] : les LR ne sont jamais recyclées, les autres oui.
        client = FakeClient(dups=[dup("wiki-lr", "LR", ["l1", "l2", "l3"]),
                                  dup("wiki-sr", "SR", ["s1", "s2", "s3"])])
        eng, client, db, path = make_engine(client, keep_spares={"LR": 0, "SR": 0},
                                            recycle_skip_rarities=["LR"])
        try:
            await eng.recycle_pass()
            self.assertFalse(any(p.startswith("l") for p in client.recycled))  # aucune LR
            self.assertTrue(all(p.startswith("s") for p in client.recycled))   # que des SR
        finally:
            db.close(); os.remove(path)

    async def test_all_copies_locked_recycles_nothing(self):
        # Si TOUS les exemplaires d'un type sont verrouillés -> 0 recyclé, tous en cooldown.
        client = FakeClient(dups=[dup("wiki-1", "SR", ["p1", "p2", "p3"])], locked={"p1", "p2", "p3"})
        eng, client, db, path = make_engine(client)
        try:
            self.assertEqual(await eng.recycle_pass(), 0)
            self.assertEqual(client.recycled, [])
        finally:
            db.close(); os.remove(path)

    async def test_marketplace_reserve_keeps_a_spare(self):
        # SR copies=2, keep_spares SR=0. Marketplace OFF -> recycle 1 ; ON -> garde 1 (recycle 0).
        dups = [dup("wiki-1", "SR", ["p1", "p2"])]
        eng, client, db, path = make_engine(
            FakeClient(dups=dups), settings=make_settings(keep_spares={"SR": 0}))
        try:
            self.assertEqual(await eng.recycle_pass(), 1)      # marketplace désactivée
        finally:
            db.close(); os.remove(path)
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-1", "SR", ["p1", "p2"])]),
            settings=Settings_with_market(keep_spares={"SR": 0}))
        try:
            self.assertEqual(await eng.recycle_pass(), 0)      # réserve marketplace -> rien
        finally:
            db.close(); os.remove(path)

    async def test_missing_mode_keeps_as_many_dups_as_missing(self):
        # Mode "missing" : on garde, PAR RARETÉ, autant de doublons qu'il manque de cartes.
        # 16 copies LR -> 15 doublons ; le catalogue a 3 LR jamais possédées (3 manquantes)
        # -> budget = 15 - 3 = 12 exemplaires recyclés.
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
        # Aucune carte manquante de la rareté -> budget = tous les doublons (copies - 1).
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["a", "b", "c", "d"])]),
            settings=make_settings(recycle_reserve_mode="missing"))
        try:
            self.assertEqual(await eng.recycle_pass(), 3)   # 4 copies -> 3 doublons, 0 manquante
        finally:
            db.close(); os.remove(path)

    async def test_benign_400_ignored_not_retried(self):
        # 400 cards_not_found (exemplaire déjà recyclé, /duplicates en retard) : on l'ignore SANS
        # le compter comme erreur à réessayer, et on le marque « terminé » (plus re-soumis).
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["a", "b", "c"])], notfound={"a"}),
            settings=make_settings(keep_spares={"SR": 0}))
        try:
            n = await eng.recycle_pass()
            self.assertEqual(n, 2)                                  # b, c recyclés ; a (400) ignoré
            self.assertEqual(client.recycled, ["b", "c"])
            self.assertIn("a", eng._recycle_done)                   # ne sera plus re-tenté
            self.assertEqual(db.recycle_failures_summary()["total"], 0)  # PAS compté comme échec 500
        finally:
            db.close(); os.remove(path)

    async def test_recycle_priority_rare_first(self):
        # "rare_first" (farm pur) : on recycle les plus rares d'abord (LR avant SR) pour extraire
        # le maximum d'encre sous le quota journalier. SR & LR ont chacune 2 doublons recyclables.
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["s1", "s2", "s3"]),
                             dup("wiki-lr", "LR", ["l1", "l2", "l3"])]),
            settings=make_settings(keep_spares={"SR": 0, "LR": 0}, recycle_priority="rare_first"))
        try:
            await eng.recycle_pass()
            self.assertEqual(client.recycled, ["l1", "l2", "s1", "s2"])   # LR d'abord
        finally:
            db.close(); os.remove(path)

    async def test_recycle_priority_common_first_default(self):
        # Défaut "common_first" : on sacrifie les moins rares d'abord (SR avant LR) — préserve les LR.
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["s1", "s2", "s3"]),
                             dup("wiki-lr", "LR", ["l1", "l2", "l3"])]),
            settings=make_settings(keep_spares={"SR": 0, "LR": 0}))
        try:
            await eng.recycle_pass()
            self.assertEqual(client.recycled, ["s1", "s2", "l1", "l2"])   # SR d'abord
        finally:
            db.close(); os.remove(path)

    async def test_quota_429_stops_pass_and_arms_cooldown(self):
        # Un 429 sur /recycle = quota JOURNALIER atteint : on stoppe la passe tout de suite et on
        # arme un cooldown (au lieu de marteler) ; la carte n'est PAS comptée comme erreur 500.
        eng, client, db, path = make_engine(
            FakeClient(dups=[dup("wiki-sr", "SR", ["a", "b", "c", "d"])], quota_after=2),
            settings=make_settings(keep_spares={"SR": 0}))
        try:
            n = await eng.recycle_pass()
            self.assertEqual(n, 2)                       # 2 recyclées, puis 429 -> stop
            self.assertEqual(client.recycled, ["a", "b"])
            self.assertGreater(eng._recycle_quota_until, time.time())     # cooldown armé
            self.assertEqual(db.recycle_failures_summary()["total"], 0)   # pas marquée 500
        finally:
            db.close(); os.remove(path)

    async def test_recycle_skipped_during_quota_cooldown(self):
        # Pendant le cooldown quota, la passe ne fait RIEN (n'appelle même pas /recycle).
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
        # Encre gagnée + plus de packs -> on réveille la boucle d'ouverture pour acheter.
        client = FakeClient(dups=[dup("wiki-1", "SR", ["p1", "p2", "p3"])])
        eng, client, db, path = make_engine(client)
        try:
            eng.resources = {"total_available": 0, "ink": 0}
            self.assertFalse(eng._wake.is_set())
            await eng.recycle_pass()
            self.assertTrue(eng._wake.is_set())   # réveil posé
        finally:
            db.close(); os.remove(path)

    async def test_target_ink_stops_when_reached(self):
        dups = [dup("wiki-1", "SR", ["p1", "p2", "p3", "p4", "p5"])]
        eng, client, db, path = make_engine(
            FakeClient(status={"ink": 0}, dups=dups), settings=make_settings(keep_spares={"SR": 0}))
        try:
            n = await eng.recycle_pass(target_ink=80)          # 40 encre/carte -> 2 cartes
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
            self.assertGreater(eng._mystery_due_at, time.time())   # réessai plus tard
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
        # Annonce honorée -> on crédite la carte voulue (plus ré-offerte) ; traité une seule fois.
        mine = [{"id": "L1", "status": "fulfilled", "wanted_card_id": "wiki-W",
                 "wanted_series_id": "s", "offered_card_type": "wiki-O", "offered_series": "s"}]
        eng, client, db, path = make_engine(FakeClient(mine=mine), settings=Settings_with_market())
        try:
            await eng.marketplace_pass()
            self.assertTrue(db.owns_card("wiki-W"))                     # crédité
            self.assertIn("L1", json.loads(db.get_kv("fulfilled_seen", "[]")))
            await eng.marketplace_pass()                                # 2e passe : pas de re-crédit
        finally:
            db.close(); os.remove(path)

    async def test_old_listing_cancelled(self):
        old_ms = (time.time() - 2 * 86400) * 1000   # créée il y a 2 jours
        mine = [{"id": "L1", "status": "active", "wanted_card_id": "wiki-W",
                 "wanted_series_id": "s", "offered_card_type": "wiki-O", "created_at": old_ms}]
        eng, client, db, path = make_engine(FakeClient(mine=mine), settings=Settings_with_market())
        try:
            await eng.marketplace_pass()
            self.assertIn("L1", client.cancelled)                      # annulée car > 1 jour
        finally:
            db.close(); os.remove(path)


def Settings_with_market(**engine_over):
    s = make_settings(**engine_over)
    s.raw["marketplace"]["enabled"] = True
    return s


if __name__ == "__main__":
    unittest.main()
