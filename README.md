# wikitcg-completer — MVP

Automatisation & suivi de complétion pour wikitcg.net : ouverture de boosters,
recyclage des doublons, et un **tableau de bord web temps réel**.

> ⚠️ Automatiser le jeu va très probablement à l'encontre des CGU de wikitcg, et la
> marketplace / le PvP classé impliquent d'autres joueurs réels. Le risque concret
> côté compte est le bannissement. À utiliser en connaissance de cause, sur ton compte.

---

## Périmètre de cette version (MVP)

| Fonction | État |
|---|---|
| Suivi de complétion par série (temps réel) | ✅ |
| Ouverture automatique de boosters (séries incomplètes priorisées) | ✅ |
| Recyclage automatique des doublons (avec réserve pour les échanges) | ✅ |
| Anti rate-limit : throttle + jitter + backoff exponentiel sur 429/5xx | ✅ |
| Persistance SQLite + historique des actions | ✅ |
| Tableau de bord web + WebSocket (réglages live, graphes, compte à rebours régén) | ✅ |
| Achat de packs à l'encre quand les gratuits sont épuisés (recyclage d'abord si besoin) | ✅ |
| Renouvellement du token de session (collage de cookie en direct, sans redémarrage) | ✅ |
| Recyclage « à la demande » + démarrage auto | ✅ |
| Priorité ouverture/achat ; la marketplace est un outil **complémentaire** (parallèle) | ✅ |
| Tâches **parallèles** (ouverture / recyclage / marketplace) à cadences découplées | ✅ |
| Annulation auto d'une annonce dès que sa carte voulue est tirée/obtenue | ✅ |
| Recyclage **carte par carte** résilient (saute les exemplaires qui renvoient 500) | ✅ |
| Ouverture du **pack mystery** (premium) dès qu'il est dispo (~1×/6h) | ✅ |
| Attente calée sur le prochain pack gratuit + **backoff croissant** (ouverture & marketplace) | ✅ |
| Réserve de doublons garantie pour la marketplace ; recyclage 500 **re-tenté après X min** | ✅ |
| Journal enrichi (cartes rares, niveau, régén, résumé marketplace, mystery…) | ✅ |
| Échanges marketplace (création/annulation/réponse d'annonces) | ✅ contrat vérifié (create/cancel testés en live) — **opt-in** car engage de vraies cartes |

### Marketplace — pool d'annonces toujours plein (phase 2, opt-in)

Activable via `[marketplace] enabled = true`. À chaque passage, le moteur maintient
**5 annonces actives en permanence** (`max_listings`) : il compte tes annonces actives
et recrée des annonces jusqu'à revenir à 5, pour ne jamais laisser un emplacement
vide pendant que les autres joueurs répondent. Pour chaque emplacement libre il vise
une carte manquante (priorité aux **séries proches de la complétion**) et offre le
doublon de la **rareté la plus faible éligible** (règle « je donne → je reçois »,
préserve les cartes de valeur). Avec `fulfill_others = true`, il honore aussi les
annonces d'autrui qui offrent une de tes cartes manquantes quand tu as un doublon à
donner. Désactivé par défaut : ces actions engagent tes cartes et touchent d'autres
joueurs réels.

## Installation

```bash
pip install -r requirements.txt
cp config.example.toml config.toml
```

Puis ouvre `config.toml` et colle la valeur de ton cookie **`wtcg_session`** dans
`api.session_cookie` (DevTools → onglet Réseau → une requête vers wikitcg.net →
en-tête `Cookie`, ou onglet Stockage → Cookies).

**Cookies supplémentaires** (ex. `cf_clearance` de Cloudflare) : renseigne
`api.extra_cookies` au format `"nom1=val1; nom2=val2"`. ⚠️ `cf_clearance` est lié à
ton User-Agent — garde le même `api.user_agent` que le navigateur d'où vient le cookie.

> 🔒 **Sécurité** : `config.toml` contient ton cookie (un JWT avec ton e-mail). Il est dans
> `.gitignore` (ne pas versionner). Tu peux aussi fournir le cookie via la variable
> d'environnement `WIKITCG_SESSION` (et `WIKITCG_EXTRA_COOKIES`), qui priment sur le TOML.

### Session & renouvellement du token

Le cookie `wtcg_session` est un **JWT d'environ 14 jours**. wikitcg **n'expose aucun endpoint
de refresh** (vérifié) et ne renvoie pas de cookie rafraîchi : on ne peut donc pas le « refaire »
par requête. Le tableau de bord affiche donc une **pastille de session** (compte à rebours
d'expiration) et, à l'approche de l'échéance ou en cas de `401`, un **bandeau** invite à coller un
cookie frais. Clique la pastille **« session »** → colle le nouveau `wtcg_session` (re-connexion
navigateur) → **effet immédiat sans redémarrage**, et c'est persisté. (Endpoint : `POST /api/auth/cookie`.)

La synchro est **rapide et progressive** : les 12 séries s'affichent tout de suite
(chiffres de `/api/collection`), puis s'affinent au fil du chargement des détails.

## Lancement

```bash
python run.py
```

Puis ouvre **http://127.0.0.1:8765**. Le bouton **Démarrer** lance la boucle
(sync → ouvre → recycle → attend → recommence), **Stop** l'arrête, **Sync**
resynchronise la collection sans rien ouvrir.

## Multi-comptes (farm séquentiel — `run_multi.py`)

Pour accumuler des doublons à **échanger contre les LR manquantes**, tu peux faire tourner
plusieurs comptes, chacun cantonné à **UNE série**. Copie `accounts.example.toml` en
`accounts.toml` et renseigne, par compte, son `session_cookie`, son `extra_cookies`
(`cf_clearance`) et la `series` à farmer. Puis :

```bash
python run_multi.py
```

**Suivi web** : pendant que ça tourne, une page read-only est servie sur
**http://127.0.0.1:8766** (port distinct du serveur mono-compte 8765) — statut de chaque
compte, série, niveau, encre, packs dispo, packs ouverts et exemplaires recyclés, le compte
actif surligné, rafraîchie toutes les 2 s. Désactivable via `[runner] web = false`
(`web_host` / `web_port` réglables).

Fonctionnement : **séquentiel** (un compte actif à la fois). Le compte ouvre sa série et
recycle ses doublons pour racheter des packs ; on l'exploite **à fond** puis on **bascule au
suivant** dès qu'il n'a plus rien à faire (plus de packs gratuits, encre insuffisante et
recyclage **plafonné** par le quota journalier ~200/j → `429`, géré automatiquement). On boucle
sur la liste ; quand **tous** les comptes sont épuisés, pause `runner.idle_cycle_minutes` puis
nouveau tour (le temps que les packs gratuits / le quota se régénèrent). Les réglages communs
(throttle, recyclage, `recycle_quota_cooldown_minutes`…) viennent de `config.toml` ; chaque
compte a sa **propre base** `wikitcg_<nom>.db`. La marketplace reste **désactivée** ici
(open + recycle) : tu réalises les échanges à la main. `accounts.toml` est gitignoré (secrets).

## Réglages utiles (`config.toml`)

- `throttle.min_interval` / `jitter` — espacement (variable) entre requêtes.
- `throttle.cooldown_every` / `cooldown_seconds` — grande pause périodique (anti-429 en rafale).
- `engine.keep_spares` — doublons **conservés** par rareté comme matière d'échange (phase 2).
  Les commons (`C`) ne s'échangent pas → `C = 0` (tout le surplus est recyclé).
- `engine.on_empty` — `wait` (attendre la régén) ou `stop` quand les packs gratuits sont épuisés.
- `engine.buy_packs_with_ink` — achète un restock à l'encre quand les packs sont épuisés
  (recycle les doublons d'abord si l'encre manque). `engine.min_ink_reserve` garde une réserve.
- `engine.recycle_mode` — `surplus` (vide tout le surplus, max d'encre) ou `on_demand`
  (recycle le minimum pour financer un restock, en sacrifiant les cartes les moins chères →
  préserve les cartes rares pour l'échange).
- `engine.mystery_pack` — ouvre le **pack mystery** (premium, raretés hautes) dès qu'il est
  disponible (~1×/6h, `engine.mystery_interval_hours`). S'ouvre comme un pack normal.
- `engine.autostart` — démarre la boucle automatiquement au lancement du serveur.

> Priorité : tant qu'on peut **ouvrir un pack ou acheter un restock**, on le fait. La marketplace
> (§ ci-dessous) n'interrompt jamais l'ouverture — c'est un outil **complémentaire** en parallèle.

Ces réglages sont aussi modifiables **en direct depuis le tableau de bord** — effet immédiat à
l'itération suivante, persistés (survivent au redémarrage). Le bouton **« Réinitialiser »** oublie
les réglages UI et revient à `config.toml` (`POST /api/config/reset`).

## Tests

**54 tests** sans réseau : fonctions pures (stratégie, marketplace, parsing, JWT), couche SQLite,
**moteur** (via un `FakeClient`) et **client HTTP** (via `httpx.MockTransport` — parsing des
doublons, retries 429/5xx, 401) :

```bash
python -m unittest discover -s tests
```

## Logique de décision (booster vs échange)

Pas de seuil codé en dur. Pour chaque carte manquante, le coût **espéré en boosters**
≈ `1 / (P(rareté) × part de cette rareté encore manquante)`. Au début d'une série,
beaucoup de manquantes communes → coût faible → **on ouvre**. En fin de série, il ne
reste que des raretés hautes et rares → le coût espéré explose (collectionneur de
coupons) → **on bascule vers l'échange ciblé**. Le point de bascule émerge donc du
croisement coût-packs / coût-échange (voir `app/strategy.py`). Les **taux de tirage**
n'étant pas connus, ils sont **appris empiriquement** à partir des ouvertures
journalisées (`pull_log`).

## Recyclage

Le recyclage lit `GET /api/cards/duplicates` — une ligne par type de carte
(`{cardId, seriesId, rarity, copies, pullIds:[…]}`, chaque `pullId` étant l'id d'un
exemplaire) — puis recycle le surplus (`copies - 1 - keep_spares[rareté]`) via
`POST /api/cards/recycle {"cardIds": [pullId…]}` (réponse `{recycled, inkEarned, newBalance}`).
Le parseur reste **tolérant** (autodétection des champs, override possible via
`[recycle] id_field / type_field / quantity_field`) et **fail-safe** : si aucun exemplaire
n'est identifié, il **ne recycle rien** et l'indique dans le journal.

## Structure

```
app/
  config.py        chargement TOML + défauts
  api_client.py    client async : throttle, backoff, taxonomie d'erreurs, endpoints
  db.py            SQLite (catalogue, inventaire, logs, analytics)
  strategy.py      sélection de série, politique de recyclage, logique d'échange (phase 2)
  engine.py        boucle d'automatisation (sync / open / recycle)
  events.py        bus pub/sub -> WebSocket
  series_seed.py   noms + tailles des 12 séries
  web.py           FastAPI : REST + WebSocket + dashboard + réglages live
  orchestrator.py  farm multi-comptes séquentiel (une série par compte, bascule à l'épuisement)
  orchestrator_web.py  page web de suivi du farm multi-comptes (port 8766, read-only)
  logging_conf.py  logs console + fichier (UTF-8, niveau réglable)
frontend/index.html  tableau de bord
docs/
  LOGIQUE.md       logique & raisonnements du moteur (à lire pour comprendre les décisions)
  AMELIORATIONS.md recherche d'améliorations priorisée
run.py               point d'entrée (serveur web mono-compte)
run_multi.py         point d'entrée (farm multi-comptes : lit accounts.toml)
accounts.example.toml  modèle de liste de comptes (à copier en accounts.toml)
```

## Documentation

- **[docs/LOGIQUE.md](docs/LOGIQUE.md)** — comment le programme décide (boucle, choix de série,
  recyclage, achat à l'encre, bascule booster↔échange, marketplace) + contrats d'API réels.
- **[docs/AMELIORATIONS.md](docs/AMELIORATIONS.md)** — pistes d'amélioration classées par priorité.

## Journalisation (`[logging]`)

Logs console + fichier rotatif `wikitcg.log` (UTF-8). `level` règle le détail
(`DEBUG`/`INFO`/`WARNING`/`ERROR`) ; `log_requests = true` trace **chaque requête API**
(méthode, route, statut, durée) — précieux pour diagnostiquer une erreur (les échecs sont
journalisés avec la route et le corps de la réponse).

## Gestion d'erreurs (extensible)

`api_client._request` centralise les retries : 429 → backoff (respecte `Retry-After`),
5xx & erreurs réseau → backoff exponentiel + jitter, 401/403 → `AuthError` (arrêt, pas
de retry), autres 4xx → `ApiError`. La boucle moteur attrape tout et ne crashe jamais
l'appli ; chaque incident est journalisé (fichier + UI). Ajouter une règle = un cas dans
`_request` ; ajouter un endpoint = une petite méthode.
