# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Layout, git & language

- This directory (`wikitcg-completer/`) is the **git repository root** and holds all the code; run every command below from here. The shell may start one level up in `WikiTCG/` (which contains only `.claude/` and `wikitcg-completer/`) — `cd wikitcg-completer` first if so.
- **`git` is not on PATH** on this machine — it lives at `C:\Users\User\AppData\Local\Programs\Git\cmd\git.exe`. Prepend that dir to `$env:Path` (or call git by full path) before any git command, e.g. `$env:Path = "C:\Users\User\AppData\Local\Programs\Git\cmd;" + $env:Path`.
- **Code, comments, docstrings, log messages and UI text are in French.** Match that when editing.
- Requires **Python 3.11+** (uses the stdlib `tomllib`); the local `venv/` (gitignored) is Python 3.13.

## Commands

```bash
# from wikitcg-completer/
pip install -r requirements.txt
cp config.example.toml config.toml          # then paste your wtcg_session cookie into api.session_cookie

python run.py                                # single-account web dashboard → http://127.0.0.1:8765
python run_multi.py                          # sequential multi-account farm (reads accounts.toml) → status page http://127.0.0.1:8766

python -m unittest discover -s tests         # full suite (no network — uses FakeClient / httpx.MockTransport)
python -m unittest tests.test_engine         # one module
python -m unittest tests.test_strategy.SelectTargetSeries.test_picks_series_with_most_missing   # one test
```

Secrets (`config.toml`, `accounts.toml`) are gitignored. The session cookie can also come from `WIKITCG_SESSION` / `WIKITCG_EXTRA_COOKIES` env vars (these win over the TOML).

## What this is

Automation + completion tracker for the trading-card site **wikitcg.net**: opens booster packs, recycles duplicates for ink, optionally trades on the marketplace, with a real-time web dashboard. It drives a third-party site's private API via a copied browser session cookie — treat the API contracts in the code (and `docs/LOGIQUE.md`) as the source of truth, since there is no official spec.

## Architecture (the parts that span files)

**Two entry points, one engine.** `run.py` serves the FastAPI dashboard (`app/web.py`) for one account. `run_multi.py` runs `app/orchestrator.py`, which farms several accounts **sequentially** (one `Engine` at a time, each pinned to a single series via `engine.only_series`, each with its own `wikitcg_<name>.db`), switching accounts when the current one is exhausted. The orchestrator builds per-account `Settings` by `_deep_merge`-ing farm overrides onto the global config.

**The Engine is a supervisor of 4 parallel asyncio tasks** (`app/engine.py` `_run` → `asyncio.gather`): `_open_loop`, `_recycle_loop`, `_marketplace_loop`, `_status_loop`. They all share **one HTTP client whose global `Throttle` serializes every request** (anti-429), so parallelism does **not** increase network throughput — it *decouples cadences* so recycling/marketplace stay responsive while opening waits for pack regeneration. Opening is always top priority; recycling and marketplace are deferred while free/bought packs remain (marketplace has a *guaranteed* minimum cadence so infinite ink-buying can't starve it). The opener sleeps on an interruptible `asyncio.Event` (`_wake`) that the recycle/status loops set when ink/packs become available. The `Engine` class is split across mixins: `ActionsMixin` (`engine_actions.py`: open pack, mystery pack, recycle) and `MarketMixin` (`engine_market.py`: marketplace pool). An `AuthError` anywhere stops the whole engine (retrying a dead cookie is pointless).

**Config has three layers, precedence low→high** (`app/config.py` + `app/web.py`): `_DEFAULTS` → `config.toml` → env vars, then at runtime **UI overrides persisted in the SQLite `kv` table** are deep-merged on top at startup. The dashboard edits a whitelist (`EDITABLE` in `web.py`) live — changes take effect on the next loop iteration and survive restart; `POST /api/config/reset` forgets them. `_deep_merge` deep-copies so mutating one `Settings` never leaks into `_DEFAULTS` or another account.

**HTTP client** (`app/api_client.py`): `_request` centralizes the retry taxonomy — 429 → backoff honoring `Retry-After`; 5xx/network → exponential backoff + jitter; 401/403 → `AuthError` (no retry); other 4xx → `ApiError`. Some calls opt out (`retry_5xx=False` for card-by-card recycling so one 500 card is skipped fast; `retry_429=False` for recycle since a 429 there means the daily quota is hit). Adding an endpoint = a small method calling `_request`; adding an error rule = a branch in `_request`. The duplicates parser is deliberately tolerant (auto-detects field names, overridable via `[recycle]`).

**Persistence** (`app/db.py`): one SQLite file per account. Beyond inventory/catalog, it *learns empirically* — `pull_log` → observed pull rates, `recycle_log` → ink value per rarity — and persists cross-restart state like recycle failures (500s to retry later), the mystery-pack timer, and the daily-quota cooldown.

**Real-time**: `app/events.py` is a tiny pub/sub `EventBus`; the engine publishes actions/status/resources, and `web.py`'s `/ws` WebSocket fans them out to the dashboard (`frontend/index.html`).

**Strategy** (`app/strategy.py`): only `select_target_series` (open the incomplete series missing the most cards) and `plan_recycle` are wired into the MVP engine. `expected_packs_for_card` / `should_trade_series` (dynamic booster↔trade switch with no hard-coded threshold) are **phase-2 scaffolding, not yet called by the engine**.

**Session/auth** (`app/auth.py`): the `wtcg_session` cookie is a ~14-day JWT and wikitcg exposes **no refresh endpoint**. Renewal is manual: paste a fresh cookie via `POST /api/auth/cookie` → `client.update_session` applies it without restart and persists it to the `kv` table. `auth.py` only decodes JWT claims (no signature check) to show an expiry countdown.

## Reference docs

- `docs/LOGIQUE.md` — decision logic (loop, series choice, recycling, ink-buying, booster↔trade switch, marketplace) and the real API contracts.
- `docs/AMELIORATIONS.md` — prioritized improvement ideas.
- `README.md` — user-facing setup and the full feature/config reference.
