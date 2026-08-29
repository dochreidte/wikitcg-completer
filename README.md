# wikitcg-completer — MVP

Automation & completion tracking for wikitcg.net: opening boosters,
recycling duplicates, and a **real-time web dashboard**.

> ⚠️ Automating the game very likely violates wikitcg's terms of service, and the
> marketplace / ranked PvP involve other real players. The concrete risk to your
> account is a ban. Use at your own risk, on your own account.

---

## Scope of this version (MVP)

| Feature | Status |
|---|---|
| Per-series completion tracking (real-time) | ✅ |
| Automatic booster opening (incomplete series prioritized) | ✅ |
| Automatic duplicate recycling (with a reserve kept for trades) | ✅ |
| Anti rate-limit: throttle + jitter + exponential backoff on 429/5xx | ✅ |
| SQLite persistence + action history | ✅ |
| Web dashboard + WebSocket (live settings, charts, regen countdown) | ✅ |
| Buying ink packs when free ones are exhausted (recycling first if needed) | ✅ |
| Session token renewal (paste a fresh cookie live, no restart) | ✅ |
| On-demand recycling + auto-start | ✅ |
| Opening/buying priority; the marketplace is a **complementary** tool (parallel) | ✅ |
| **Parallel** tasks (opening / recycling / marketplace) at decoupled cadences | ✅ |
| Auto-cancel of a listing as soon as its wanted card is pulled/obtained | ✅ |
| Resilient **card-by-card** recycling (skips copies that return 500) | ✅ |
| Opening the **mystery pack** (premium) as soon as it's available (~1×/6h) | ✅ |
| Wait aligned to the next free pack + **increasing backoff** (opening & marketplace) | ✅ |
| Guaranteed duplicate reserve for the marketplace; 500 recycling **retried after X min** | ✅ |
| Enriched log (rare cards, level, regen, marketplace summary, mystery…) | ✅ |
| Marketplace trades (creating/cancelling/answering listings) | ✅ contract verified (create/cancel tested live) — **opt-in** since it commits real cards |

### Marketplace — listing pool always full (phase 2, opt-in)

Enabled via `[marketplace] enabled = true`. On every pass, the engine keeps
**5 listings active at all times** (`max_listings`): it counts your active listings
and recreates listings until it's back up to 5, so a slot is never left
empty while other players respond. For each free slot it targets
a missing card (prioritizing **series close to completion**) and offers the
lowest-rarity eligible duplicate (the "I give → I receive" rule,
preserving valuable cards). With `fulfill_others = true`, it also fulfills
others' listings that offer one of your missing cards when you have a duplicate to
give. Disabled by default: these actions commit your cards and affect other
real players.

## Installation

```bash
pip install -r requirements.txt
cp config.example.toml config.toml
```

Then open `config.toml` and paste the value of your **`wtcg_session`** cookie into
`api.session_cookie` (DevTools → Network tab → a request to wikitcg.net →
`Cookie` header, or Storage tab → Cookies).

**Extra cookies** (e.g. Cloudflare's `cf_clearance`): set
`api.extra_cookies` in the format `"name1=val1; name2=val2"`. ⚠️ `cf_clearance` is tied to
your User-Agent — keep the same `api.user_agent` as the browser the cookie came from.

> 🔒 **Security**: `config.toml` contains your cookie (a JWT with your email). It's in
> `.gitignore` (don't commit it). You can also provide the cookie via the
> `WIKITCG_SESSION` environment variable (and `WIKITCG_EXTRA_COOKIES`), which take precedence over the TOML.

### Session & token renewal

The `wtcg_session` cookie is a **JWT lasting about 14 days**. wikitcg **exposes no refresh
endpoint** (verified) and doesn't return a refreshed cookie: it therefore can't be "remade"
via a request. The dashboard thus shows a **session badge** (expiry
countdown) and, as the deadline approaches or on a `401`, a **banner** prompts you to paste a
fresh cookie. Click the **"session"** badge → paste the new `wtcg_session` (browser
re-login) → **immediate effect with no restart**, and it's persisted. (Endpoint: `POST /api/auth/cookie`.)

Syncing is **fast and progressive**: the 12 series show up right away
(figures from `/api/collection`), then refine as the details load.

## Running

```bash
python run.py
```

Then open **http://127.0.0.1:8765**. The **Start** button launches the loop
(sync → open → recycle → wait → repeat), **Stop** stops it, **Sync**
resyncs the collection without opening anything.

## Multi-account (sequential farm — `run_multi.py`)

To accumulate duplicates to **trade for the missing LRs**, you can run
several accounts, each confined to **ONE series**. Copy `accounts.example.toml` to
`accounts.toml` and provide, per account, its `session_cookie`, its `extra_cookies`
(`cf_clearance`) and the `series` to farm. Then:

```bash
python run_multi.py
```

**Web tracking**: while it runs, a read-only page is served at
**http://127.0.0.1:8766** (a separate port from the single-account server's 8765) — the status of each
account, its series, level, ink, available packs, packs opened and copies recycled, the active
account highlighted, refreshed every 2 s. Can be disabled via `[runner] web = false`
(`web_host` / `web_port` are configurable).

How it works: **sequential** (one active account at a time). The account opens its series and
recycles its duplicates to buy more packs; we exploit it **to the fullest** then **switch to the
next one** as soon as it has nothing left to do (no more free packs, insufficient ink, and
recycling **capped** by the daily quota ~200/day → `429`, handled automatically). We loop
over the list; when **all** accounts are exhausted, it pauses for `runner.idle_cycle_minutes` then
starts a new round (giving free packs / the quota time to regenerate). The shared settings
(throttle, recycling, `recycle_quota_cooldown_minutes`…) come from `config.toml`; each
account has its **own database** `wikitcg_<name>.db`. The marketplace stays **disabled** here
(open + recycle): you make trades by hand. `accounts.toml` is gitignored (secrets).

## Useful settings (`config.toml`)

- `throttle.min_interval` / `jitter` — (variable) spacing between requests.
- `throttle.cooldown_every` / `cooldown_seconds` — periodic long pause (anti-429 on bursts).
- `engine.keep_spares` — duplicates **kept** per rarity as trade material (phase 2).
  Commons (`C`) aren't traded → `C = 0` (all the surplus is recycled).
- `engine.on_empty` — `wait` (wait for regen) or `stop` when free packs are exhausted.
- `engine.buy_packs_with_ink` — buys an ink restock when packs are exhausted
  (recycles duplicates first if ink is short). `engine.min_ink_reserve` keeps a reserve.
- `engine.recycle_mode` — `surplus` (empties all the surplus, max ink) or `on_demand`
  (recycles the minimum to fund a restock, sacrificing the cheapest cards →
  preserves rare cards for trading).
- `engine.mystery_pack` — opens the **mystery pack** (premium, high rarities) as soon as it's
  available (~1×/6h, `engine.mystery_interval_hours`). Opens like a normal pack.
- `engine.autostart` — starts the loop automatically when the server launches.

> Priority: as long as we can **open a pack or buy a restock**, we do. The marketplace
> (see below) never interrupts opening — it's a **complementary** tool running in parallel.

These settings are also editable **live from the dashboard** — they take effect immediately on the
next iteration, persisted (they survive restart). The **"Reset"** button forgets
the UI settings and reverts to `config.toml` (`POST /api/config/reset`).

## Tests

**66 tests** with no network: pure functions (strategy, marketplace, parsing, JWT), the SQLite layer,
the **engine** (via a `FakeClient`) and the **HTTP client** (via `httpx.MockTransport` — duplicate
parsing, 429/5xx retries, 401):

```bash
python -m unittest discover -s tests
```

## Decision logic (booster vs trade)

No hard-coded threshold. For each missing card, the **expected cost in boosters**
≈ `1 / (P(rarity) × share of that rarity still missing)`. Early in a series,
lots of common cards are missing → low cost → **we open**. Late in a series, only
the high and rare rarities are left → the expected cost explodes (coupon
collector) → **we switch to targeted trading**. The switch point thus emerges from the
booster-cost / trade-cost crossover (see `app/strategy.py`). Since the **pull rates**
aren't known, they're **learned empirically** from the logged
openings (`pull_log`).

## Recycling

Recycling reads `GET /api/cards/duplicates` — one row per card type
(`{cardId, seriesId, rarity, copies, pullIds:[…]}`, each `pullId` being the id of one
copy) — then recycles the surplus (`copies - 1 - keep_spares[rarity]`) via
`POST /api/cards/recycle {"cardIds": [pullId…]}` (response `{recycled, inkEarned, newBalance}`).
The parser stays **tolerant** (auto-detects fields, can be overridden via
`[recycle] id_field / type_field / quantity_field`) and **fail-safe**: if no copy
can be identified, it **recycles nothing** and notes it in the log.

## Structure

```
app/
  config.py        TOML loading + defaults
  api_client.py    async client: throttle, backoff, error taxonomy, endpoints
  db.py            SQLite (catalog, inventory, logs, analytics)
  strategy.py      series selection, recycling policy, trade logic (phase 2)
  engine.py        automation loop (sync / open / recycle)
  events.py        pub/sub bus -> WebSocket
  series_seed.py   names + sizes of the 12 series
  web.py           FastAPI: REST + WebSocket + dashboard + live settings
  orchestrator.py  sequential multi-account farm (one series per account, switch on exhaustion)
  orchestrator_web.py  web page tracking the multi-account farm (port 8766, read-only)
  logging_conf.py  console + file logs (UTF-8, adjustable level)
frontend/index.html  dashboard
run.py               entry point (single-account web server)
run_multi.py         entry point (multi-account farm: reads accounts.toml)
accounts.example.toml  template account list (copy to accounts.toml)
```

## Logging (`[logging]`)

Console logs + a rotating file `wikitcg.log` (UTF-8). `level` sets the detail
(`DEBUG`/`INFO`/`WARNING`/`ERROR`); `log_requests = true` traces **every API request**
(method, route, status, duration) — invaluable for diagnosing an error (failures are
logged with the route and the response body).

## Error handling (extensible)

`api_client._request` centralizes retries: 429 → backoff (honors `Retry-After`),
5xx & network errors → exponential backoff + jitter, 401/403 → `AuthError` (stop, no
retry), other 4xx → `ApiError`. The engine loop catches everything and never crashes
the app; each incident is logged (file + UI). Adding a rule = a case in
`_request`; adding an endpoint = a small method.
