# Rapport complet & analyse à froid — wikitcg-completer

_Analyse objective de l'état du projet, du fonctionnement, des forces et des points à
réparer/améliorer (avec plusieurs pistes par point)._

---

## 1. Inventaire des fichiers

### 1.1 Répertoire parent — `…\Projects\Python\`
Contient 14 dossiers de projets indépendants ; **un seul** concerne ici : `WikiTCG`.
Les autres (`AI_news`, `bingo_Noel`, `EBOOK`, `fod wwebsite`, `GT`, `hello-fresh_dump`,
`json_to_csv`, `Recipe Website`, `Test XML`, `tts`, `Website`, `web_site_avocat`, `202020`)
sont **hors périmètre** (aucun lien avec ce projet).

### 1.2 Mon répertoire de travail — `…\Python\WikiTCG\`
| Élément | Nature | Remarque |
|---|---|---|
| `wikitcg-completer/` | le projet | code réel (voir §1.3) |
| `.claude/` | config/mémoire de l'assistant | non lié au runtime |
| `*.json` (12 fichiers) | **captures HAR** Firefox des appels réels | **précieux** : contrats d'API (voir §1.4) |
| `wikitcg.db` (64 Ko) | base SQLite **obsolète** | doublon hors projet, à supprimer |
| `wikitcg.log` (359 o) | log **obsolète** | idem |

> ⚠️ La vraie base et le vrai log vivent dans `wikitcg-completer/` (l'app tourne depuis là).
> Les `wikitcg.db`/`wikitcg.log` à la racine de `WikiTCG/` sont des résidus à nettoyer.

### 1.3 Le projet — `…\WikiTCG\wikitcg-completer\`
```
run.py                10 l   point d'entrée uvicorn
app/
  config.py          107 l   TOML + défauts + env + overrides UI
  api_client.py      339 l   client HTTP : throttle, backoff, taxonomie d'erreurs, endpoints, cookies
  db.py              345 l   SQLite (catalogue, inventaire, logs, échecs recyclage, analytics)
  engine.py          666 l   ⚠ LE plus gros : 3 boucles parallèles + toutes les actions
  strategy.py         95 l   fonctions PURES (choix série, coût pack/échange)
  marketplace.py      94 l   fonctions PURES (plan annonces / réponses)
  events.py           24 l   bus pub/sub → WebSocket
  web.py             275 l   FastAPI : REST + WS + dashboard + réglages live
  logging_conf.py     45 l   logs console+fichier (UTF-8, niveau réglable)
  series_seed.py      21 l   noms + tailles des 12 séries
  auth.py             36 l   décodage JWT + état d'expiration de session
frontend/index.html  632 l   tableau de bord (1 fichier, JS inline)
docs/                        LOGIQUE.md, AMELIORATIONS.md, RAPPORT.md (ce fichier)
tests/                       test_strategy/marketplace/helpers (35 tests, fonctions pures)
config.toml / config.example.toml / .gitignore / requirements.txt / README.md
venv/                        environnement Python 3.13
wikitcg.db                   base active (séries 12, catalog 2217, inventory 1824,
                             pull_log 445, recycle_log 41, actions 235, snapshots 90)
wikitcg.log                  log actif
```

### 1.4 Captures HAR (contrats d'API confirmés)
Les 12 `*.json` à la racine de `WikiTCG/` sont des exports HAR DevTools = **vérité terrain**.
Tous **concordent** avec le code actuel :

| Capture | Endpoint | Contrat |
|---|---|---|
| `status.json` | `GET /api/nav/status` | `{ink, freePacks, totalAvailable, nextRegenAt, maxFreePacks, level, xp, streak…}` |
| `series_progress_global.json` | `GET /api/collection` | `[{series_id, owned, total_pulls}]` |
| `series_progress_spe.json` | `GET /api/collection/{sid}` | `[{card_id, rarity, quantity, first_pulled}]` |
| `cards_by_series.json` | `GET /api/series/{sid}/cards` | `{cards:[{id, title, …}]}` |
| `open_packs.json` | `POST /api/packs/open` | `{seriesId}` → cartes |
| `by_restock_with_ink.json` | `POST /api/packs/regen` | `{type:"full"}` → `{success, newBalance, freePacks, totalAvailable}` |
| `recycle_cards.json` | `POST /api/cards/recycle` | `{cardIds:[unId]}` → `{recycled, inkEarned, newBalance}` |
| `create-marketplace.json` | `POST /api/marketplace/create` | `{offeredCardTypeId, offeredSeriesId, wantedCardId, wantedSeriesId}` → 201 |
| `acceptl_offer_marketplace.json` | `POST /api/marketplace/{id}/fulfill` | → `{success, newAchievements}` |
| `cancel_offer_marketplace.json` | `POST /api/marketplace/{id}/cancel` | → `{success}` |
| `all_offer_marketplace.json` | `GET /api/marketplace` | `{listings:[…]}` |
| `status_listings_marketplace.json` | `GET /api/marketplace/mine` | `{listings:[…]}` |

> Le recyclage capté envoie **un seul `cardId`** par requête (comme le code) ; le 500
> n'est donc PAS un problème de forme/headers mais d'**exemplaires verrouillés** (voir §4.2).

---

## 2. Fonctionnement simplifié

**But** : compléter les collections en pilote automatique. Cycle auto-alimenté :
`packs → cartes (+ doublons) → encre (recyclage) → packs (rachat)`.

Au démarrage : `full_sync()` (statut + collection + inventaire + catalogue). Puis **3 tâches
asyncio en parallèle** partageant un même client HTTP (le *throttle* sérialise les requêtes →
anti-429 ; la parallélisation ne fait que **découpler les cadences**) :

1. **Ouverture** (`_open_loop`) — priorité absolue : ouvre les packs présents (série la plus
   incomplète) ; pack **mystery** premium toutes les ~6 h ; plus de packs → **achète** un restock
   à l'encre (recycle d'abord pour financer si besoin) ; sinon **attend** jusqu'au prochain pack
   gratuit (`nextRegenAt`) ou backoff croissant plafonné.
2. **Recyclage** (`_recycle_loop`, ~30 s) — recycle le surplus **carte par carte**, par rareté
   croissante, en gardant une réserve pour la marketplace ; saute les 500 (mémoire persistée).
3. **Marketplace** (`_marketplace_loop`, ~45 s, opt-in) — maintient N annonces actives, honore
   les offres utiles, annule les annonces dont la carte est déjà obtenue ; backoff si rien à faire.

**Dashboard** : WebSocket (push instantané) + **rafraîchissement /api/live toutes les 5 s** ;
réglages modifiables en direct (persistés) ; session (cookie JWT) renouvelable à chaud.

---

## 3. Ce qui fonctionne BIEN

- **Contrats d'API exacts** : tout est vérifié contre les captures HAR réelles (§1.4).
- **Client HTTP robuste** : throttle 2 régimes, backoff exponentiel + jitter sur 429/5xx/réseau,
  taxonomie d'erreurs claire, trace par requête, gestion fine des cookies (anti-`CookieConflict`).
- **Cœur métier pur & testé** : `strategy.py` et `marketplace.py` sont sans I/O et couverts par
  **35 tests** verrouillant les règles (ne pas offrir une carte unique ; n'honorer que si
  doublon possédé + carte non possédée ; surplus de recyclage ; etc.).
- **Recyclage résilient** : un par un, saute les exemplaires non recyclables (500) et les
  **mémorise en base** (`recycle_failures`) avec réessai temporisé → survit au redémarrage.
- **Parallélisme propre** : arrêt coopératif (`running`), annulation en cascade via `gather`,
  une `AuthError` stoppe tout, chaque boucle isole ses erreurs.
- **Boucle auto-alimentée** : ouverture → recyclage → encre → rachat, sans intervention.
- **Dashboard temps réel** : WS + poll 5 s, graphes, compte à rebours de régén, contrôles live,
  renouvellement de session sans redémarrage.
- **Config solide** : défauts + TOML + variables d'env + overrides UI persistés, bouton reset.

---

## 4. Ce qu'il faudrait RÉPARER / AMÉLIORER (plusieurs pistes par point)

### P1 — Sécurité : secret en clair (cookie JWT + cf_clearance)
**Constat.** `config.toml` contient un JWT de session valide (avec e-mail) et un `cf_clearance`.
Gitignoré, mais en clair sur le disque, et déjà manipulé en session.
**Pistes.**
- (a) **Roter le token** maintenant (se déconnecter/reconnecter sur le site).
- (b) Lire le cookie depuis **`WIKITCG_SESSION`** (déjà supporté) et retirer la valeur du TOML.
- (c) Chiffrer le secret au repos (ex. DPAPI Windows / `keyring`) et le déchiffrer au démarrage.

### P2 — `engine.py` trop gros (666 l, responsabilités mêlées)
**Constat.** Orchestration + ouverture + recyclage + marketplace + mystery + achat + boucles
dans un seul fichier → difficile à tester/maintenir.
**Pistes.**
- (a) **Découper par domaine** : `engine/opener.py`, `engine/recycler.py`, `engine/market.py`,
  `engine/supervisor.py` (chacun une responsabilité, l'orchestrateur les compose).
- (b) Extraire un **`actions.py`** (open/recycle/buy/mystery purs vis-à-vis de l'I/O) testable
  avec un faux client.
- (c) A minima, **regrouper les constantes/réglages** et documenter les invariants en tête.

### P3 — Dérive de synchronisation (inventaire/séries)
**Constat.** `full_sync()` ne tourne qu'au **démarrage**. Si tu joues en parallèle dans le
navigateur, l'inventaire/les séries du bot **divergent** jusqu'au prochain redémarrage ; les
compteurs de packs/encre sont optimistes entre deux `sync_status`.
**Pistes.**
- (a) **Re-sync périodique léger** : rappeler `GET /api/collection` (1 appel) toutes les N min
  pour rafraîchir owned/total sans recharger tout le détail.
- (b) **Full re-sync** complet espacé (ex. toutes les 30 min) en tâche dédiée.
- (c) **Sur demande** : un bouton « Resync » existe déjà ; ajouter un resync auto quand l'UI
  est ouverte et le moteur à l'arrêt.

### P4 — Recyclage 500 : exemplaires verrouillés non détectables
**Constat.** Certains exemplaires renvoient 500 (verrouillés côté serveur — deck/état non
exposé). Aucune API ne liste lesquels. Géré par essai→saut→réessai temporisé (persisté), mais
on « gaspille » une tentative par carte verrouillée à chaque expiration du cooldown.
**Pistes.**
- (a) **Backoff par exemplaire** : augmenter `retry_at` selon `fail_count` (déjà stocké) →
  30 min, puis 2 h, puis 6 h… pour les cartes durablement verrouillées.
- (b) **Trouver l'endpoint front du recyclage** (page `/missions`) pour voir si l'UI dispose
  d'un filtre « recyclable » (un GET dédié) qu'on pourrait répliquer.
- (c) **Cap d'essais** : au-delà de K échecs, ne plus retenter (liste noire), avec purge manuelle.

### P5 — Couverture de tests limitée au cœur pur
**Constat.** `engine`, `api_client`, `web`, `db` ne sont pas testés automatiquement (seules les
fonctions pures le sont). Beaucoup de changements récents sans tests d'intégration.
**Pistes.**
- (a) **FakeClient + fixtures HAR** : transformer les `*.json` (§1.4) en réponses simulées et
  tester `engine`/`api_client` **hors-ligne** (idéal, données déjà réelles).
- (b) **Tests DB** sur fichier temporaire (recycle_failures, progress_view, decrement…).
- (c) **httpx MockTransport** pour rejouer les contrats sur `api_client` sans réseau.

### P6 — Marketplace : `fulfill` non exécuté en réel (irréversible)
**Constat.** `create`/`cancel` vérifiés en live ; `fulfill` confirmé **par la capture HAR**
(`acceptl_offer_marketplace.json` → 200) mais jamais exécuté par le bot (échange irréversible).
**Pistes.**
- (a) **Test unique encadré** : honorer une offre à faible enjeu, puis vérifier l'inventaire.
- (b) **Mode « dry-run »** : journaliser les échanges qu'on ferait sans les exécuter, pour
  validation avant activation réelle.
- (c) Laisser `fulfill_others=false` par défaut (déjà le cas) et le réserver à un usage manuel.

### P7 — Affichage = état du moteur (pas la vérité serveur quand à l'arrêt)
**Constat.** `/api/state` et `/api/live` renvoient `engine.resources`, figé quand le moteur est
arrêté.
**Pistes.**
- (a) Quand le moteur est **à l'arrêt**, faire faire à `/api/live` un `sync_status()` paresseux
  (avec petit cache anti-spam, ex. 15 s).
- (b) Afficher dans l'UI un repère « données du … » (horodatage de dernière synchro).
- (c) Bouton « Resync » plus visible quand `running=false`.

### P8 — Persistance des réglages UI qui masque le TOML
**Constat.** Une fois un réglage changé dans l'UI, il prime sur `config.toml` (kv overrides) ;
éditer le TOML pour cette clé n'a plus d'effet (surprenant). `autostart=true` a ainsi été activé.
**Pistes.**
- (a) **Badge « réglé via l'UI »** par champ + bouton « Réinitialiser » (existe déjà globalement,
  le rendre par-clé).
- (b) Afficher la **source effective** de chaque réglage (TOML / env / UI).
- (c) Option « ne rien persister » (mode éphémère) pour les essais.

### P9 — Verbosité des logs & rotation
**Constat.** Niveau `DEBUG` + `log_requests=true` → logs très volumineux (chaque requête).
Utile en diagnostic, lourd en continu (rotation 2 Mo × 5).
**Pistes.**
- (a) **Profil de log réglable depuis l'UI** (INFO ↔ DEBUG) sans redémarrage.
- (b) **Log structuré JSON** optionnel (ingestion/filtrage facile).
- (c) Augmenter la rétention (`maxBytes`/`backupCount`) si on garde DEBUG longtemps.

### P10 — Hygiène du dépôt
**Constat.** `wikitcg.db`/`wikitcg.log` obsolètes à la racine de `WikiTCG/` ; captures HAR non
rangées ; pas de tests CI.
**Pistes.**
- (a) **Supprimer** les résidus racine ; déplacer les HAR dans `wikitcg-completer/tests/fixtures/`.
- (b) Ajouter un petit **CI** (GitHub Actions) lançant `python -m unittest`.
- (c) `Makefile`/script `run-tests` + `lint` (ruff) pour l'hygiène.

---

## 5. Priorisation (quick wins → fond)
1. **Roter le token** + passer en variable d'env (P1) — 10 min, sécurité.
2. **Nettoyer les résidus racine** + ranger les HAR en fixtures (P10).
3. **Tests d'intégration via FakeClient + HAR** (P5) — filet durable, données déjà disponibles.
4. **Re-sync périodique léger** (P3) — exactitude des infos.
5. **Backoff par exemplaire** pour le recyclage 500 (P4).
6. **Découpage d'`engine.py`** (P2) — dette technique, à faire à froid.

---

## 6. Verdict
Le projet est **fonctionnel, cohérent et bien découplé sur le cœur métier** ; les contrats
d'API sont exacts (vérifiés contre les captures réelles) et la boucle d'automatisation est
robuste (parallélisme, backoff, recyclage résilient persistant, dashboard live). Les principaux
axes d'amélioration sont : la **sécurité du secret**, la **dette technique d'`engine.py`**, la
**fraîcheur des données** (re-sync périodique) et la **couverture de tests** (facile à étendre
grâce aux captures HAR déjà présentes).
