"""Multi-account orchestrator: sequential farm of ONE series per account.

Goal: for EACH account, open packs of its series (`engine.only_series`) and recycle the
duplicates to re-buy packs with ink, IN A LOOP, until no more packs can be bought with ink —
then move on to the next account. Trades (LR) are handled by hand, outside this program.

Execution model: SEQUENTIAL — only one account active at a time. The engine loops
open -> (no more packs) -> recycle the MINIMUM (rare-first) to fund -> re-buy a restock
-> open ... We recycle as little as possible, sacrificing the RAREST cards first (maximum
ink per recycle, daily quota preserved); the surplus is NOT drained. When it can NO LONGER
open a pack with ink (no free packs AND not enough ink even after recycling), it goes to the
`waiting` status: that is the SWITCH signal to the next account. We loop over the list
indefinitely; when a full pass produces NO openings (all accounts exhausted), we wait
`idle_cycle_minutes` before starting again (time for free packs / the recycle quota to
regenerate).

No marketplace here (open + recycle only): trades remain manual.

Run: `python run_multi.py`  (reads accounts.toml at the project root).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .api_client import WikiTCGClient
from .auth import is_real_session
from .config import Settings, _deep_merge, load_settings
from .db import Database
from .engine import Engine
from .events import EventBus

log = logging.getLogger("wikitcg.orchestrator")


@dataclass
class Account:
    name: str
    series: str
    session_cookie: str
    extra_cookies: str = ""        # e.g. "cf_clearance=..."
    mystery: bool = False          # also open the mystery pack (separate pool) — default no
    db_path: str = ""              # dedicated SQLite database (per-account inventory)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "account"


def load_accounts(path: str = "accounts.toml") -> tuple[list[Account], dict]:
    """Read accounts.toml -> (list of valid accounts, [runner] options).

    Expected shape:
        [runner]
        poll_seconds = 8.0
        idle_cycle_minutes = 30.0

        [[account]]
        name = "..."; series = "..."; session_cookie = "..."; extra_cookies = "..."
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy accounts.example.toml to accounts.toml and fill in your accounts.")
    with open(p, "rb") as fh:
        data = tomllib.load(fh)

    runner = dict(data.get("runner", {}) or {})
    accounts: list[Account] = []
    seen: dict[str, int] = {}
    for i, raw in enumerate(data.get("account", []) or [], start=1):
        name = str(raw.get("name") or f"account{i}").strip()
        series = str(raw.get("series") or "").strip()
        session = str(raw.get("session_cookie") or "").strip()
        if not series or not is_real_session(session):
            log.warning("Account \"%s\" skipped: series and session_cookie are required.", name)
            continue
        # Duplicate names (frequent copy-paste): we do NOT drop them — we disambiguate so each
        # account gets a distinct database (wikitcg_<slug>.db / <slug>-2.db...) and still runs.
        slug = _slug(name)
        seen[slug] = seen.get(slug, 0) + 1
        uniq = slug if seen[slug] == 1 else f"{slug}-{seen[slug]}"
        label = name if seen[slug] == 1 else f"{name}#{seen[slug]}"
        accounts.append(Account(
            name=label, series=series, session_cookie=session,
            extra_cookies=str(raw.get("extra_cookies") or "").strip(),
            mystery=bool(raw.get("mystery", False)),
            db_path=f"wikitcg_{uniq}.db",
        ))
    return accounts, runner


class MultiAccountRunner:
    """Runs the accounts one after another (see module header)."""

    def __init__(self, accounts: list[Account], base_settings: Settings, *,
                 poll_seconds: float = 8.0, idle_cycle_minutes: float = 30.0):
        self.accounts = accounts
        self.base = base_settings
        self.poll = max(float(poll_seconds), 1.0)
        self.idle_cycle = max(float(idle_cycle_minutes), 0.0) * 60
        self.running = True
        # MANUAL switch (from the monitoring page): _skip = leave the active account on the next
        # tick; _goto = name of the account to activate next (None -> simply the next one).
        self._skip = False
        self._goto: str | None = None
        # Live state for the monitoring page (read-only).
        self.active: str | None = None      # currently active account
        self.cycle = 0                       # pass number over the list
        self.started_at = time.time()
        self.stats: dict[str, dict] = {
            a.name: {"series": a.series, "status": "pending", "active": False,
                     "opened": 0, "recycled": 0, "ink": None, "packs": None, "level": None}
            for a in accounts
        }

    # ------------------------------------------------------------------ #
    async def _sleep_poll(self) -> None:
        """Sleep up to `self.poll` s, but wake almost immediately if a manual switch is
        requested (so the monitoring page button reacts quickly)."""
        step = 0.5
        waited = 0.0
        while waited < self.poll and self.running and not self._skip:
            await asyncio.sleep(min(step, self.poll - waited))
            waited += step

    def _settings_for(self, acc: Account) -> Settings:
        """Account settings: global base + overrides (cookies, series, farm guardrails)."""
        over = {
            "api": {"session_cookie": acc.session_cookie, "extra_cookies": acc.extra_cookies},
            "engine": {
                "only_series": acc.series,        # open ONLY this series
                "mystery_pack": acc.mystery,
                "buy_packs_with_ink": True,        # the farm re-buys packs with recycled ink
                "auto_open": True, "auto_recycle": True,
                # Tightest possible farm loop: open the packs, then recycle the STRICT MINIMUM
                # (and no more) to fund the next restock, sacrificing the RAREST cards first
                # (rare-first) -> MAXIMUM ink per recycle and daily quota (~200/day) preserved.
                # We re-open the restock and start again: open -> recycle the minimum -> re-buy
                # -> open.
                # NB: the site only sells the full restock (5 packs / 400 ink) — buying a single
                # pack does not exist, so the loop's re-buy unit is 1 restock.
                "recycle_mode": "on_demand",       # no surplus draining: recycle as needed
                "recycle_priority": "rare_first",  # recycle the rarest first -> max ink/card
                "on_empty": "wait",                # -> `waiting` status = switch signal
                "autostart": False,
            },
            "marketplace": {"enabled": False},     # open + recycle only
            "paths": {"database": acc.db_path, "log_file": self.base.paths["log_file"]},
        }
        return Settings(raw=_deep_merge(self.base.raw, over))

    async def _run_account(self, acc: Account) -> bool:
        """Run an account until exhaustion (`waiting` status) or error.
        Returns True if the account opened at least one pack during the visit."""
        settings = self._settings_for(acc)
        client = WikiTCGClient(settings)
        client.session_sink = lambda tok, n=acc.name: log.info(
            "Session cookie refreshed for \"%s\" — remember to update it in accounts.toml.", n)
        db = Database(acc.db_path)
        engine = Engine(client, db, EventBus(), settings)
        st = self.stats[acc.name]
        st.update(active=True, status="starting")
        self.active = acc.name
        log.info("==== Account \"%s\" -> series %s (db=%s) ====", acc.name, acc.series, acc.db_path)
        engine.start()

        def _refresh() -> None:   # copy the engine state into the monitoring stats
            r = engine.resources or {}
            st.update(status=engine.status, opened=engine.opened_total,
                      recycled=engine.recycled_total, ink=r.get("ink"),
                      packs=r.get("total_available"), level=r.get("level"))

        try:
            # Let the engine start (sync), then watch its status. We switch as soon as it goes
            # to waiting (nothing left to do) or stops (finished / cookie error).
            while self.running and engine.running:
                await self._sleep_poll()
                _refresh()
                if self._skip:
                    log.info("Account \"%s\": manual switch requested — %d opened. %s",
                             acc.name, engine.opened_total,
                             f"Activating \"{self._goto}\"." if self._goto
                             else "Moving to the next account.")
                    break
                if engine.status == "waiting":
                    log.info("Account \"%s\": no more pack buyable with ink (free packs exhausted "
                             "+ not enough ink even after recycling) — %d opened. "
                             "Moving to the next account.", acc.name, engine.opened_total)
                    break
                if engine.status == "error":
                    log.warning("Account \"%s\" in error (%s) — moving to the next.",
                                acc.name, engine.auth_error or "see logs")
                    break
        finally:
            _refresh()
            st["active"] = False
            st["status"] = ("error" if engine.status == "error"
                            else "exhausted" if engine.status == "waiting" else "stopped")
            if self.active == acc.name:
                self.active = None
            await engine.stop()
            await client.aclose()
            db.close()
        return engine.opened_total > 0

    async def run(self) -> None:
        if not self.accounts:
            log.error("No valid account in accounts.toml — nothing to do.")
            return
        log.info("Multi-account orchestrator: %d account(s) — %s",
                 len(self.accounts), ", ".join(f"{a.name}:{a.series}" for a in self.accounts))
        while self.running:
            self.cycle += 1
            worked_any = False
            idx = 0
            while idx < len(self.accounts) and self.running:
                # Manual switch: if an account was explicitly chosen, jump to it.
                if self._goto is not None:
                    target, self._goto = self._goto, None
                    j = next((k for k, a in enumerate(self.accounts) if a.name == target), None)
                    if j is not None:
                        idx = j
                acc = self.accounts[idx]
                try:
                    worked_any = await self._run_account(acc) or worked_any
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Unexpected error on account \"%s\" — continuing.", acc.name)
                self._skip = False  # switch request consumed
                # If a target was set during the visit, handle it at the top of the loop
                # (without advancing); otherwise move to the next account in the list.
                if self._goto is None:
                    idx += 1
            if not self.running:
                break
            # Full pass with no openings -> all accounts are exhausted: wait (regeneration of
            # free packs / reset of the recycle quota).
            if not worked_any and self.idle_cycle > 0:
                mins = int(self.idle_cycle / 60)
                log.info("All accounts are exhausted — pausing %d min before a new pass.", mins)
                # Interruptible pause: a manual switch (monitoring page) cuts it short.
                waited = 0.0
                while waited < self.idle_cycle and self.running and not self._skip:
                    await asyncio.sleep(min(2.0, self.idle_cycle - waited))
                    waited += 2.0
                # `_skip` was used to cut the pause (no active account to leave): we consume it
                # here so we don't immediately skip the first account of the next pass.
                # `_goto` is kept so the jump to the chosen account still happens.
                self._skip = False

    def request_switch(self, account: str | None = None) -> dict:
        """Request a MANUAL switch (called by the monitoring page).

        - `account` given  -> leave the active account, then activate THAT account.
        - `account` None   -> leave the active account and simply move to the next.
        Taken into account on the next monitoring tick (<= ~0.5 s)."""
        if account is not None:
            account = account.strip()
            known = {a.name for a in self.accounts}
            if account and account not in known:
                return {"ok": False, "error": f"unknown account: {account}"}
            self._goto = account or None
        self._skip = True
        return {"ok": True, "goto": self._goto, "from": self.active}

    def stop(self) -> None:
        self.running = False

    def status_snapshot(self) -> dict:
        """Current state for the monitoring page (JSON-serializable)."""
        return {
            "active": self.active,
            "cycle": self.cycle,
            "uptime_s": int(time.time() - self.started_at),
            "idle_cycle_min": int(self.idle_cycle / 60),
            "pending_switch": self._goto if self._skip else None,
            "accounts": [dict(name=a.name, **self.stats.get(a.name, {})) for a in self.accounts],
        }


async def run_from_config(accounts_path: str = "accounts.toml") -> None:
    """Load the global config + accounts.toml and start the orchestrator (+ monitoring page)."""
    base = load_settings()
    accounts, runner = load_accounts(accounts_path)
    orch = MultiAccountRunner(
        accounts, base,
        poll_seconds=float(runner.get("poll_seconds", 8.0)),
        idle_cycle_minutes=float(runner.get("idle_cycle_minutes", 30.0)),
    )
    if not bool(runner.get("web", True)):
        await orch.run()
        return
    # Monitoring page (read-only) in parallel, on a port distinct from the single-account
    # server (8765).
    import uvicorn
    from .orchestrator_web import make_app
    host = str(runner.get("web_host", "127.0.0.1"))
    port = int(runner.get("web_port", 8766))
    server = uvicorn.Server(uvicorn.Config(make_app(orch), host=host, port=port, log_level="warning"))
    log.info("Multi-account monitoring page: http://%s:%d", host, port)
    web_task = asyncio.create_task(server.serve())
    try:
        await orch.run()
    finally:
        server.should_exit = True
        try:
            await web_task
        except asyncio.CancelledError:
            pass
