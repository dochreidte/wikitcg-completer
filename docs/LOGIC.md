# Logique du programme & raisonnements

Ce document explique **comment** `wikitcg-completer` prend ses décisions et **pourquoi**.
Il complète le `README.md` (installation, périmètre) en détaillant le cœur du moteur.

---

## 1. Vue d'ensemble

Le but : **compléter les collections** de wikitcg.net en pilote automatique, le plus
efficacement possible, sans se faire limiter (rate-limit) ni gâcher de ressources.

Trois ressources entrent en jeu :

| Ressource | Rôle | D'où elle vient |
|---|---|---|
| **Packs gratuits** | s'ouvrent pour tirer des cartes | régénèrent dans le temps (max 5) |
| **Encre** | sert **uniquement** à racheter un restock de packs | gagnée en **recyclant** les doublons |
| **Doublons** | recyclés → encre, ou échangés (marketplace) | tirages multiples d'une même carte |

La boucle transforme donc en continu : `packs → cartes (+ doublons) → encre → packs …`
C'est un **moteur de complétion auto-alimenté**.

### Modules

```
run.py            point d'entrée (lance uvicorn)
app/
  config.py       chargement TOML + défauts + overrides persistés (UI)
  api_client.py   client HTTP async : throttle, backoff, taxonomie d'erreurs, endpoints
  db.py           SQLite : catalogue, inventaire, logs, analytics
  strategy.py     fonctions PURES : choix de série, politique de recyclage, bascule pack↔échange
  marketplace.py  fonctions PURES : planification des annonces / des réponses
  engine.py       orchestration : init, sync, achat, boucles parallèles, contrôle (start/stop)
  engine_actions.py  ActionsMixin : ouverture de packs, pack mystery, recyclage
  engine_market.py   MarketMixin  : création/réponse/annulation d'annonces (marketplace)
  events.py       bus pub/sub → WebSocket
  web.py          FastAPI : REST + WebSocket + dashboard + réglages live
  logging_conf.py logs console + fichier (UTF-8, niveau réglable)
frontend/index.html  tableau de bord temps réel
```

Découpage volontaire : **`strategy.py` et `marketplace.py` sont purs** (aucun réseau,
aucune base) → testables et auditables isolément. `engine.py` ne fait qu'**orchestrer**.

---

## 2. Les boucles principales (`engine._run` → 4 tâches PARALLÈLES)

Après `full_sync()`, le moteur lance **quatre tâches asyncio concurrentes** (`asyncio.gather`)
qui partagent le même client HTTP :

```
full_sync()                              # état + collection + inventaire + catalogue
gather(
  _open_loop()          # ouvre les packs présents ; sinon achète à l'encre ou attend
  _recycle_loop()       # recycle le surplus (DIFFÉRÉ tant qu'il reste des packs à ouvrir)
  _marketplace_loop()   # maintient les annonces (DIFFÉRÉ tant qu'il reste des packs ; si activé)
  _status_loop()        # re-sync léger encre/packs toutes les status_sync_seconds (≈15 s)
)
```

**Pourquoi paralléliser ?** Le **throttle global sérialise déjà toutes les requêtes HTTP**
(anti‑429) : la parallélisation **n'augmente pas le débit réseau**, elle **découple les
cadences**.

**Priorité à l'ouverture.** Le recyclage et la marketplace **se mettent en pause tant qu'il
reste des packs à ouvrir** (`total_available > 0`) : sinon leurs requêtes une-par-une
monopoliseraient le throttle et retarderaient l'ouverture. Ils s'exécutent donc surtout quand
les packs sont épuisés (le recyclage finance alors les achats). Le `_status_loop` garde l'encre
et le nombre de packs **à jour en continu** (et réveille l'ouverture dès qu'une régénération
apporte des packs).

`_open_loop` (résumé) :

```
while running:
    if packs_locaux == 0: sync_status()      # relire le compte réel (régén possible)
    if packs == 0:
        si buy_packs_with_ink: acheter un restock (recycler pour financer si besoin)
        sinon on_empty=="stop" → stop ; sinon attendre idle_poll_seconds
    si pack mystery dû (≥6h): open_one("mystery") ; continue   # premium, prioritaire
    target = select_target_series(progress)   # série la plus rentable
    si target is None: "tout est complet 🎉" ; stop
    open_one(target)                          # ouvre 1 booster (toujours les packs présents)
    tous les resync_every tours: sync_status()
```

### Points clés de coordination

- **Resync défensif** : le compteur de packs est optimiste ; quand il tombe à 0 on **relit**
  `/api/nav/status` (régén possible) — et `open_one` lit directement `totalAvailable` de la
  réponse d'ouverture quand l'API le fournit.
- **Acheter avant d'attendre** : si l'encre suffit, on rachète tout de suite ; sinon on
  **recycle juste assez** (mode `on_demand`) pour financer, puis on re-tente.
- **Attente intelligente + réveil** : quand il n'y a plus rien à faire côté ouverture (ni pack,
  ni encre, ni recyclage finançable), on attend **jusqu'au prochain pack gratuit connu**
  (`nextRegenAt`) ; si inconnu, **backoff croissant plafonné** (`idle_poll_seconds` →
  `idle_poll_max`). **MAIS** l'attente est **interruptible** : dès que la tâche de recyclage
  gagne assez d'encre (packs vides), elle pose un `asyncio.Event` qui **réveille l'ouverture**
  pour acheter aussitôt — vraie coordination entre tâches (plus de « j'ai l'encre mais je n'achète pas »).
- **Backoff marketplace** : si une passe ne crée/honore/annule **rien**, la boucle marketplace
  s'espace progressivement (`marketplace_interval` → `marketplace_interval_max`) ; cadence rapide
  rétablie dès qu'une action a lieu. Évite de re-sonder le marché des autres pour rien.
- **Pack mystery** : pack premium (raretés hautes garanties) ouvert **comme un pack normal**
  (`POST /api/packs/open {seriesId:"mystery"}` — 0 encre, consomme 1 pack dispo) mais limité à
  **~1×/6h** (gating serveur). Le moteur le tente dès qu'il est dû et qu'un pack est libre, en
  **priorité** sur les séries normales. L'échéance (`mystery_due_at`) est persistée (kv) ; sur
  refus/cooldown, nouvel essai dans ~30 min. Réglable : `engine.mystery_pack` (+ toggle UI).
- **Annulation immédiate sur tirage** : dès qu'un pack donne une carte qu'une de mes annonces
  visait (`wanted_card_id`), `open_one` **annule cette annonce** (via le cache `_my_listings`).
  `marketplace_pass` fait aussi un balayage périodique (annule toute annonce dont la carte
  voulue est désormais possédée, quel qu'en soit le chemin d'acquisition).
- **Arrêt coopératif** : `self.running=False` fait sortir les trois boucles ; `stop()` annule la
  tâche superviseur, ce qui annule les trois sous-tâches (via `gather`). Une `AuthError` stoppe
  tout (réessayer est inutile : cookie expiré).

---

## 3. Choix de la série à ouvrir (`strategy.select_target_series`)

> On ouvre la série **incomplète où il manque le PLUS de cartes** (départage : plus faible %).

```python
incomplete = [séries où total>0 et missing>0]
return max(incomplete, key=lambda p: (p["missing"], -p["pct"]))
```

**Raisonnement** : dans une série où il manque beaucoup de cartes, *presque chaque tirage est
utile* (forte proba de tomber sur une carte non possédée). À mesure qu'une série se remplit,
les nouveaux packs tombent surtout sur des doublons → rendement décroissant. Prioriser la série
la moins complète maximise donc le nombre de **cartes neuves par pack**.

C'est une heuristique simple et robuste. La section 6 décrit une logique plus fine (non câblée).

---

## 4. Politique de recyclage (`engine.recycle_pass`)

Source : `GET /api/cards/duplicates` → une ligne **par type de carte** :

```json
{ "cardId": "wiki-…", "seriesId": "…", "rarity": "SR",
  "copies": 5, "pullIds": ["<hex exemplaire>", …], "title": "…" }
```

Chaque `pullId` est l'identifiant d'un **exemplaire** précis. Pour chaque type :

```
surplus = copies - 1 (l'exemplaire de collection) - keep_spares[rareté]
candidats = `surplus` premiers pullIds, triés par valeur CROISSANTE (sacrifier les moins
            précieux d'abord), plafonné à recycle.max_per_call réussites par passe
```

**Deux modes de réserve** (`engine.recycle_reserve_mode`) :
- **`"fixed"`** (défaut) : réserve par carte = `keep_spares[rareté]` (+ plancher marketplace),
  comme la formule ci-dessus.
- **`"missing"`** : on garde, **par rareté**, autant de doublons qu'il **manque** de cartes de
  cette rareté (toutes séries confondues) — la matière d'échange dont on a réellement besoin pour
  les acquérir. `budget[rareté] = max(Σ doublons[rareté] − manquantes[rareté], 0)` ; le recyclage
  est plafonné à ce budget, raretés traitées de la moins rare à la plus rare. Ex. 15 LR en double
  et 3 LR manquantes → on en recycle 12. `keep_spares` et le plancher marketplace sont ignorés.

**Recyclage exemplaire par exemplaire, avec repli sur les autres copies** : pour chaque TYPE,
on vise `surplus` recyclages et on essaie ses pullIds **un par un** ; si l'un renvoie 500
(verrouillé), on tente **un autre exemplaire du même type** au lieu de gâcher le quota — c'est
ce qui fait que le bot recycle bien tout le surplus recyclable (comme le site).
On appelle `POST /api/cards/recycle {"cardIds":[pullId]}` **une carte à la fois**. Certaines cartes renvoient un **500** (ex. exemplaire engagé dans un
deck) ; on les **saute aussitôt** (sans les 5 retries habituels) et on tente la suivante — au lieu
de faire échouer tout le lot (c'était la cause du « recyclage cassé en 500 »). Une carte fautive
est mise en cooldown et **re-tentée après `recycle_retry_minutes`** (défaut 30 min) — elle peut
redevenir recyclable (sortie d'un deck…). Cette mémoire est **persistée en base**
(table `recycle_failures` : `pull_id`, `card_id`, `rarity`, `fail_count`, `retry_at`) → elle
**survit au redémarrage** (pas de re-tentative inutile des cartes verrouillées) et est rechargée
au démarrage du moteur. Le dashboard affiche le nombre de cartes non recyclables + le prochain
essai. Réponse par carte recyclée : `{recycled, inkEarned, newBalance}` (l'encre est cumulée).

**Réserve pour la marketplace** : si la marketplace est activée, on garde **toujours ≥ 1 doublon
par rareté échangeable** (jamais tout recyclé) — il faut de la matière pour les annonces. La
réserve effective par rareté = `max(keep_spares[rareté], 1)` quand la marketplace est ON.

**Raisonnements clés** :

- **`keep_spares`** garde une réserve par rareté comme **matière d'échange** (phase marketplace).
  Les communs `C` ne s'échangent pas → `keep_spares.C = 0` (tout le surplus est recyclé).
- **Fail-safe** : si la réponse ne contient aucun exemplaire identifiable (`pull_ids` vide
  partout), on **ne recycle rien** et on l'écrit dans le journal — jamais d'action à l'aveugle.
- **Fait observé** : `/api/cards/duplicates` ne renvoie que les raretés **SR et au-dessus**.
  Les doublons C/UC/R ne sont donc pas recyclables par cette voie (et leur valeur d'encre est
  inconnue, d'où `recycle.values` C/UC/R = 0). Le recyclage cible naturellement la valeur haute.

> ⚠️ Effet de bord : avec marketplace désactivée, recycler le surplus SR+/UR/LR est la seule
> façon de générer de l'encre. C'est voulu (« l'encre ne sert qu'à racheter des packs »), mais
> cela **consomme** des cartes rares en trop. La réserve `keep_spares` protège le minimum.

---

## 5. Achat de packs à l'encre (`engine._try_buy_packs`)

```
coût = packs.full_restock_ink (400)   ;   réserve = engine.min_ink_reserve (0)
si encre < coût + réserve: ne rien faire
sinon: POST /api/packs/regen {"type":"full"}  → {success, newBalance, freePacks, totalAvailable}
```

**Raisonnement** : l'encre n'a qu'un seul usage utile pour compléter — racheter des packs.
On la dépense donc dès qu'on en a assez (en gardant une réserve optionnelle réglable depuis
l'UI). Couplé au recyclage automatique, cela donne la boucle auto-alimentée du §1.

---

## 6. Coût espéré booster vs échange (échafaudage, `strategy.py`)

Cette logique (`expected_packs_for_card` / `should_trade_series`) est **écrite et testée mais
NON branchée** pour décider quoi faire. **Choix de conception explicite** (consigne) :
**l'ouverture et l'achat de recharges sont prioritaires** — tant qu'on peut ouvrir un pack ou
acheter un restock, on le fait. On n'**arrête jamais** d'ouvrir pour « attendre un échange » ;
la marketplace n'est qu'un **outil complémentaire** qui tourne en parallèle (§7). La fonction
reste disponible comme indicateur / pour un usage futur.

```
coût espéré (en packs) d'une carte précise manquante
  ≈ 1 / [ 1 - (1 - p_carte)^cartes_par_pack ]
avec p_carte = P(rareté) × (manquantes_de_cette_rareté / total) / total
```

**Intuition** (« collectionneur de coupons ») : en début de série, beaucoup de manquantes
communes → coût faible → **on ouvre**. En fin de série il ne reste que du rare et peu de cibles
→ le coût espéré **explose** → il devient plus rentable d'**échanger** une carte précise.
`should_trade_series` bascule quand le coût médian dépasse l'équivalent-packs d'un échange.

Les `P(rareté)` ne sont pas connus a priori : ils sont **appris empiriquement** depuis les
tirages journalisés (`pull_log` → `Database.empirical_pull_rates`).

---

## 7. Marketplace (opt-in — `marketplace.py` + `engine.marketplace_pass`)

**Rôle** : outil **complémentaire** pour compléter les collections, qui tourne en **tâche
parallèle** sans jamais interrompre l'ouverture/l'achat (prioritaires). Objectif : garder
**en permanence `max_listings` annonces actives** (par défaut 5) pour ne jamais laisser un
emplacement vide pendant que les autres joueurs répondent.

- **Règle d'échange** « je DONNE (offered) → je REÇOIS (wanted) », par rareté
  (`TRADE_GIVE_TO_GET`). On offre toujours la rareté éligible **la plus faible** (préserve les
  cartes de valeur). Les `C` ne s'offrent pas.
- **`plan_listings`** (créer mes annonces) : pour chaque emplacement libre, vise une carte
  manquante (priorité aux séries **proches de la complétion**).
  **Focalisation** (`marketplace.near_completion_max_missing`, 0 = off) : si > 0, on ne cible
  **que** les séries à qui il manque ≤ N cartes — l'effort d'échange se concentre sur les séries
  qu'on peut réellement finir bientôt, au lieu de s'éparpiller sur des séries à peine entamées.
  **Invariant 1 — n'offrir que des cartes en plusieurs exemplaires** : disponible =
  `quantity - 1 - déjà_engagé`, et `dups` ne contient que les cartes `quantity ≥ 2` → on garde
  toujours ≥ 1 exemplaire, jamais d'offre d'une carte unique.
- **`plan_fulfillments`** (`fulfill_others`, honorer les annonces d'autrui) —
  **Invariant 2 — honorer SEULEMENT si les deux conditions sont vraies** : (a) je possède la carte
  **demandée** en double (`spare = quantity-1 ≥ 1`, je garde 1) **et** (b) je ne possède **pas
  déjà** la carte **offerte** (elle est dans mes manquantes).

Ces deux invariants sont verrouillés par des tests (`tests/test_marketplace.py`).

**État des contrats** (vérifiés) :

| Endpoint | Méthode | Vérifié |
|---|---|---|
| `/api/marketplace/mine` | GET | ✅ champs confirmés |
| `/api/marketplace` (browse) | GET | ✅ |
| `/api/marketplace/create` | POST | ✅ live (201) — `{offeredCardTypeId, offeredSeriesId, wantedCardId, wantedSeriesId}` |
| `/api/marketplace/{id}/cancel` | POST | ✅ live (200) |
| `/api/marketplace/{id}/fulfill` | POST | ✅ contrat (frontend) — non exécuté car irréversible |

> Note : `create` envoie un **TYPE** de carte (`offeredCardTypeId` = `card_id`) ; c'est le serveur
> qui engage un exemplaire en trop. (Contrat lu dans le frontend Astro du site, puis testé en live
> par un cycle create→cancel.) Désactivé par défaut : engage de vraies cartes et d'autres joueurs.

Désactivée par défaut (`[marketplace] enabled = false`) car elle **engage de vraies cartes** et
**touche d'autres joueurs réels**. Le dashboard l'affiche en **lecture seule** (mes annonces +
le marché) via `GET /api/marketplace`.

---

## 8. Robustesse réseau (`api_client`)

- **Throttle global** à deux régimes : lectures (GET) rapides ; actions (POST) plus espacées,
  avec une **grande pause périodique** toutes `cooldown_every` ouvertures (anti-429 en rafale).
- **Backoff** exponentiel + jitter sur **429** (respecte `Retry-After`), **5xx** et erreurs
  **réseau**. Plafonné à `backoff_cap`.
- **Taxonomie d'erreurs** qui dicte le comportement :
  - `AuthError` (401/403) → **arrêt** (un retry n'y changera rien : cookie expiré).
  - `RateLimited` (429) → interne, déclenche le backoff.
  - `ApiError` (autres 4xx, ou 5xx/réseau persistant) → remontée au moteur, qui journalise.
- **Logs** : chaque échec est tracé avec **méthode + route + statut + corps** ; en
  `[logging] log_requests = true`, *chaque* requête est tracée (méthode, route, statut, durée).

Ajouter un endpoint = une petite méthode appelant `_request`. Ajouter une règle d'erreur =
un cas dans `_request`.

---

## 9. Modèle de données (`db.py`)

- **Complétion d'une série** = `nb lignes inventory(série)` / `total`, où `total` =
  `set_size` (graine `series_seed.py`) ou, à défaut, la taille du catalogue chargé.
- Avant le chargement du détail, on affiche un **indice** (`owned_hint`/`pulls_hint` issus de
  `/api/collection`) pour un rendu immédiat, affiné ensuite par l'inventaire exact.
- `pull_log` et `recycle_log` alimentent l'**apprentissage empirique** (taux de tirage,
  valeur d'encre par rareté) et les graphiques du dashboard.
- `kv` stocke les **réglages modifiés depuis l'UI** (`settings_overrides`), réappliqués au
  démarrage (priment sur le TOML).

---

## 10. Contrats d'API réels (vérifiés en live)

```
GET  /api/nav/status        → {ink, freePacks, paidPacks, totalAvailable, maxFreePacks,
                               nextRegenAt, level, xp, xpToNext, xpProgress, streak, …}
GET  /api/collection        → [{series_id, owned, total_pulls}]
GET  /api/collection/{sid}  → détail par carte (inventaire exact)
GET  /api/series/{sid}/cards→ catalogue (set complet)
POST /api/packs/open {seriesId}        → {cards:[…], series:{…}, xpEarned, streak}
       (seriesId="mystery" = pack premium, ~1×/6h, 0 encre, consomme 1 pack)
GET  /api/cards/duplicates  → {duplicates:[{cardId, seriesId, rarity, copies, pullIds:[…], title}]}
POST /api/cards/recycle {cardIds:[pullId…]} → {recycled, inkEarned, newBalance}
POST /api/packs/regen {type:"full"}    → {success, newBalance, freePacks, totalAvailable}
GET  /api/marketplace[/mine]→ {listings:[{id, status, offered_card_type, offered_series,
                               offered_rarity, offered_card_id, wanted_card_id,
                               wanted_series_id, lister_name, …}]}
POST /api/marketplace/create {offeredCardTypeId, offeredSeriesId, wantedCardId, wantedSeriesId} → 201 {success}
POST /api/marketplace/{id}/cancel   → 200 {success}
POST /api/marketplace/{id}/fulfill  → {success, …}  (échange irréversible)
GET  /api/auth/me           → {sub, email, name, …}   (seul endpoint d'auth ; aucun /refresh)
```

---

## 11. Session & renouvellement du token

Le cookie `wtcg_session` est un **JWT Google-OAuth d'environ 14 jours**. Constats (vérifiés) :

- **Aucun endpoint de refresh** : `/api/auth/refresh`, `/token`, `/renew`, `/extend`… → 404.
  Seul `/api/auth/me` existe (lecture). On **ne peut pas** régénérer le jeton par requête.
- **Pas de cookie glissant** : les réponses ne renvoient pas de `Set-Cookie` rafraîchi.

Conséquence : le seul vrai « refresh » est de **se reconnecter dans le navigateur** et coller le
nouveau cookie. Le programme rend ça indolore :

1. **Lecture de l'expiration** (`app/auth.py`, sans vérif de signature) → exposée dans
   `/api/state.token` et via une **pastille** UI (compte à rebours).
2. **Mise à jour à chaud** : `POST /api/auth/cookie` (champ dans l'UI) remplace le cookie du
   client **sans redémarrage** (`WikiTCGClient.update_session`) et le **persiste** (`kv`).
3. **Priorité des sources** : variable d'env `WIKITCG_SESSION` > override UI persisté > `config.toml`.
4. **Capture défensive** : si un jour le serveur renvoyait un `Set-Cookie`, il est capté et persisté
   (`session_sink`). Inutile aujourd'hui, mais sans risque.
5. **Alerte** : à l'approche de l'expiration (< 2 j) ou sur `401` (`AuthError`), un bandeau invite
   à coller un cookie frais ; le moteur s'arrête proprement sur `AuthError` (pas de retry inutile).
