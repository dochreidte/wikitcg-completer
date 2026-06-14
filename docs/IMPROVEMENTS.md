# Recherche d'améliorations

Analyse du projet à un instant T, classée par **priorité**. Chaque point indique le
constat, le risque/bénéfice, et une piste concrète. Référence des fichiers entre crochets.

Légende priorité : **P1** correctness/sécurité · **P2** efficacité · **P3** confort/maintenance.

## Mises en œuvre — chantier complet (« go tout faire »)

Tous les axes du rapport ont été traités :

| Réf | Action | Statut |
|---|---|---|
| P1 | Token via `WIKITCG_SESSION` (prioritaire) + `.gitignore` + hint sécurité au démarrage | ✅ (rotation = action utilisateur) |
| P2 | `engine.py` **découpé** : `engine.py` (660→395 l) + `engine_actions.py` (ActionsMixin) + `engine_market.py` (MarketMixin) | ✅ |
| P3 | Re-sync complet **périodique** opt-in (`engine.full_resync_minutes`, 0 = off) | ✅ |
| P4 | Recyclage 500 : **backoff par exemplaire croissant** (selon `fail_count`, ×1→×8) | ✅ |
| P5 | **Filet de tests** : `tests/` = strategy, marketplace, helpers, db, **engine (FakeClient)**, **api_client (httpx MockTransport)** → 54 tests | ✅ |
| P6 | Marketplace **dry-run** (`[marketplace] dry_run`) : journalise sans écrire | ✅ |
| P7 | `/api/live` fait un **sync paresseux** (cache 15 s) quand le moteur est à l'arrêt | ✅ |
| P8 | Indicateur de **source des réglages** (forcés via l'UI) sur le label « Réglages » | ✅ |
| P9 | **Niveau de log réglable depuis l'UI** (`POST /api/logging`, persisté) + toggle trace requêtes | ✅ |
| P10 | Résidus racine supprimés ; fixtures de test via FakeClient (sans secret) | ✅ |
| + | Mémoire des échecs de recyclage **persistée** (`recycle_failures`) ; rafraîchissement UI 5 s | ✅ |

> ⚠️ La **rotation du token** reste à faire côté utilisateur (action navigateur).

## État d'avancement (itération précédente)

| # | Sujet | Statut |
|---|---|---|
| 1.1 | Marketplace : écritures | ✅ `create`/`cancel` **vérifiés en live** (201/200) ; `fulfill` confirmé par le frontend (non exécuté car irréversible) |
| 1.2 | Secrets en clair | ✅ `.gitignore` + variable d'env `WIKITCG_SESSION` (rotation = action utilisateur) |
| 1.3 | Diagnostic des 500 | ✅ logs route+statut+corps ; `[logging] log_requests` |
| 1.4 | Session expirée dans l'UI | ✅ pastille + bandeau + collage de cookie en direct |
| 1.5 | Overrides UI masquent le TOML | ✅ persiste les clés modifiées seulement + bouton « Réinitialiser » |
| 2.1 | Bascule booster↔échange | ⛔ retirée volontairement : ouvrir/acheter est PRIORITAIRE, la marketplace ne bloque jamais l'ouverture |
| 2.2 | Recyclage à la demande | ✅ `recycle_mode = on_demand` |
| 2.3 | Données apprises → décisions | 🟡 taux empiriques utilisés par 2.1 ; valeurs de recyclage encore informatives |
| 2.4 | Compteurs optimistes | ✅ lit `totalAvailable` de la réponse d'ouverture si présent |
| 3.1 | Tests | ✅ `tests/` (stratégie, marketplace, helpers, JWT) |
| 3.4 | Autostart | ✅ `engine.autostart` |
| + | Refresh de token | ✅ mécanisme manuel en direct (wikitcg n'a pas d'endpoint de refresh) |

---

## P1 — Correctness & sécurité

### 1.1 — Marketplace : écritures (✅ vérifiées)
**Résolu.** Le contrat a été lu directement dans le frontend du site puis testé en live :
- `create` = `POST /api/marketplace/create {offeredCardTypeId, offeredSeriesId, wantedCardId,
  wantedSeriesId}` — on offre un **TYPE** de carte (le serveur engage un exemplaire). Testé →
  **201 `{success:true}`**, annonce retrouvée via `/mine`, puis `cancel` → **200 `{success:true}`**
  (aucune annonce résiduelle).
- `cancel` = `POST /api/marketplace/{id}/cancel`.
- `fulfill` = `POST /api/marketplace/{id}/fulfill` (confirmé par le frontend ; **non exécuté en
  test car irréversible** — un échange honoré donne une carte à un autre joueur).

Le code (`api_client.marketplace_create`, `engine.marketplace_pass`) utilise ce contrat vérifié.
Activable depuis l'UI (toggles « Auto-annonces » / « Honorer les offres », avec confirmation) ou
`[marketplace] enabled`. Reste **opt-in** par prudence (engage de vraies cartes, vrais joueurs).

### 1.2 — Identifiants réels (cookie/JWT) en clair dans `config.toml`
**Constat.** `config.toml` contient un **JWT de session valide** (avec l'e-mail de l'utilisateur)
et un `cf_clearance`. Le fichier vit en clair sur le disque.
**Risque.** Fuite d'identifiants (quiconque lit le fichier prend la session). Si le dossier est
un jour versionné/partagé, c'est une compromission.
**Piste.** (a) S'assurer que `config.toml` est dans `.gitignore` (seul `config.example.toml`
sans secret est versionné — y mettre un placeholder, pas un vrai token). (b) Permettre de lire
le cookie depuis une **variable d'environnement** (`WIKITCG_SESSION`) en priorité. (c) **Faire
tourner (rotate) le token actuellement exposé** par sécurité.

### 1.3 — Diagnostic des 500 (désormais possible — à exploiter)
**Constat.** Les anciens logs montrent une rafale `serveur 500` persistante (5 essais → abandon)
sans la route ni le corps. Le logging a été corrigé : on trace maintenant
`⚠ MÉTHODE /route → 500 : <corps>` [`api_client._request`].
**Piste.** Si les 500 reviennent, lancer avec `[logging] log_requests = true` pour voir la
séquence exacte. Vérifier si c'est lié à un endpoint précis (payload), à un pic de débit
(throttle trop agressif) ou à une instabilité serveur (transitoire → le backoff suffit).

### 1.4 — Session expirée mal signalée dans l'UI
**Constat.** Le bandeau n'alerte que si le cookie est **vide** [`web.get_state` → `has_session`].
Un cookie **expiré** déclenche `AuthError` (le moteur s'arrête, statut `error`) mais l'UI ne dit
pas clairement « reconnecte-toi ».
**Piste.** Exposer le dernier `AuthError` dans `/api/state` et afficher un bandeau dédié
« session expirée — renouvelle `wtcg_session` ».

### 1.5 — Persistance des réglages UI masque le TOML
**Constat.** Dès qu'on touche **un** réglage dans l'UI, **tous** les réglages éditables sont
figés dans `kv.settings_overrides` et **priment** sur `config.toml` au démarrage
[`web.set_config`]. Ensuite, éditer ces clés dans le TOML **n'a plus d'effet** (surprenant).
**Piste.** Ne persister que les clés réellement modifiées ; afficher un indicateur
« réglé via l'UI » + un bouton « réinitialiser au config.toml » (efface l'override).

---

## P2 — Efficacité de la complétion

### 2.1 — Bascule booster↔échange : **retirée volontairement**
**Décision (consigne).** Ouvrir des packs et acheter des recharges est **prioritaire** : tant que
c'est possible, on le fait. On n'interrompt **jamais** l'ouverture pour « attendre un échange ».
La marketplace est un **outil complémentaire** qui tourne en parallèle (§ marketplace). Le
branchement qui stoppait l'ouverture en fin de série a donc été **supprimé** de `_open_loop`.
`strategy.should_trade_series` reste disponible (testé) comme indicateur, sans piloter la boucle.

### 2.2 — Recyclage : arbitrage encre ⇄ matière d'échange
**Constat.** On recycle tout le surplus SR+ au-delà de `keep_spares` à chaque passage, ce qui
**détruit des cartes rares** précieuses pour les échanges.
**Piste.** Recycler **juste assez** pour financer le prochain restock (coût + réserve), plutôt
que vider tout le surplus, surtout si la marketplace est active. Politique « recycle on demand ».

### 2.3 — Les données apprises ne pilotent pas (encore) les décisions
**Constat.** `empirical_pull_rates` et `empirical_recycle_values` sont calculés et affichés mais
ne ré-alimentent pas le choix de série ni la valeur de recyclage.
**Piste.** Injecter ces taux dans §2.1 et utiliser les valeurs d'encre apprises pour prioriser
le recyclage des raretés les plus rentables d'abord.

### 2.4 — Compteurs de ressources optimistes
**Constat.** `open_one` décrémente les packs localement sans confirmation serveur ; un
resync périodique recale [`engine._run` étape 7]. En cas d'échec partiel, léger risque de
désynchro temporaire.
**Piste.** Sur réponse de `/api/packs/open`, lire le nombre de packs restants si l'API le
renvoie, plutôt que de décrémenter à l'aveugle.

---

## P3 — Confort & maintenabilité

### 3.1 — Aucun test automatisé
**Constat.** `strategy.py` et `marketplace.py` sont **purs** → idéaux pour des tests unitaires ;
`api_client`/`engine` se testent avec un faux client.
**Piste.** Ajouter `pytest` + un `FakeClient` (réponses figées issues des contrats du
`docs/LOGIQUE.md` §10). Couvrir : sélection de série, surplus de recyclage, plans marketplace,
taxonomie d'erreurs (429/5xx/401).

### 3.2 — Modèles de réponse typés
**Constat.** Parsing tolérant par dictionnaires/`_pick`. Robuste, mais peu auto-documenté.
**Piste.** Introduire de petits dataclasses/pydantic pour `Status`, `Duplicate`, `Listing` —
clarifie les champs et attrape les régressions d'API.

### 3.3 — Marketplace rechargée à chaque ouverture du dashboard
**Constat.** `GET /api/marketplace` fait 2 appels live à chaque chargement de page.
**Piste.** Cache court (ex. 30 s) côté serveur, ou chargement uniquement au clic « Actualiser ».

### 3.4 — Reprise après redémarrage
**Constat.** Le moteur ne redémarre pas tout seul (Start manuel) — volontaire.
**Piste.** Option `engine.autostart` pour relancer la boucle au boot si souhaité.

### 3.5 — Logs exploitables par machine
**Constat.** Logs texte lisibles (bien) ; pas de format structuré.
**Piste.** Option de sortie JSON (1 objet/ligne) pour ingestion/alerting si besoin un jour.

---

## Quick wins (effort faible, valeur haute)

1. **`.gitignore` + token rotation** (§1.2) — sécurité, 10 min.
2. **Bandeau « session expirée »** (§1.4) — évite les arrêts silencieux.
3. **Probe de vérification marketplace create** (§1.1) — débloque la phase 2 en sécurité.
4. **Tests unitaires `strategy`/`marketplace`** (§3.1) — filet de sécurité durable.
5. **Recyclage « on demand »** (§2.2) — préserve les cartes rares.

## Checklist avant d'activer la marketplace

- [ ] Vérifier `create` avec 1 annonce de test → ajuster le payload (exemplaire vs type).
- [ ] Vérifier `fulfill` avec 1 échange à faible enjeu.
- [ ] Confirmer le format de réponse (champ statut, id) et l'expiration des annonces.
- [ ] Décider de la politique recycle vs réserve d'échange (§2.2).
- [ ] Mettre `[marketplace] enabled = true` seulement après ces vérifications.
