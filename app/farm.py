"""Farm: runs the configured accounts one after another, one engine at a time."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from . import vault
from .api_client import WikiTCGClient
from .auth import token_status
from .config import Settings, _deep_merge
from .db import Database
from .engine import Engine
from .events import EventBus

log = logging.getLogger("wikitcg.farm")

_EMPTY_STATS = {"status": "idle", "opened": 0, "recycled": 0, "ink": None, "packs": None, "level": None}


@dataclass
class Session:
    account: dict
    settings: Settings
    client: object
    db: Database
    engine: Engine


class Farm:
    def __init__(self, app_db: Database, settings: Settings, bus: EventBus, *,
                 client_factory=WikiTCGClient, engine_factory=Engine, db_factory=Database,
                 poll_seconds: float = 8.0, idle_cycle_seconds: float = 1800.0):
        self.app_db = app_db
        self.settings = settings
        self.bus = bus
        self._client_factory = client_factory
        self._engine_factory = engine_factory
        self._db_factory = db_factory
        self.poll = poll_seconds
        self.idle_cycle = idle_cycle_seconds
        self.session: Session | None = None
        self.running = False
        self.cycle = 0
        self.stats: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        self._goto: str | None = None
        self._wake = asyncio.Event()

    def accounts(self) -> list[dict]:
        return self.app_db.list_accounts()

    def current_id(self) -> str | None:
        return self.session.account["id"] if self.session else None

    def _account_raw(self, acc: dict) -> dict:
        return _deep_merge(self.settings.raw, {
            "api": {"session_cookie": vault.get(acc["id"], "session"),
                    "extra_cookies": vault.get(acc["id"], "extra")},
            "engine": {"only_series": acc["series"],
                       "only_series_mode": "strict" if acc["series"] else "fallback"},
            "paths": {"database": acc["db_path"]},
        })

    def refresh_settings(self) -> None:
        if self.session:
            self.session.settings.raw = self._account_raw(self.session.account)

    def _refresh_stats(self) -> None:
        s = self.session
        if not s:
            return
        r = s.engine.resources or {}
        self.stats[s.account["id"]] = {
            "status": s.engine.status, "opened": s.engine.opened_total,
            "recycled": s.engine.recycled_total, "ink": r.get("ink"),
            "packs": r.get("total_available"), "level": r.get("level"),
        }

    async def _close_session(self) -> None:
        s, self.session = self.session, None
        if not s:
            return
        if s.engine.running:
            await s.engine.stop()
        await s.client.aclose()
        s.db.close()

    async def _open(self, account_id: str) -> None:
        acc = self.app_db.get_account(account_id)
        if acc is None:
            raise KeyError(account_id)
        await self._close_session()
        settings = Settings(raw=self._account_raw(acc))
        client = self._client_factory(settings)
        client.session_sink = lambda tok: vault.store(acc["id"], "session", tok)
        db = self._db_factory(acc["db_path"])
        engine = self._engine_factory(client, db, self.bus, settings)
        self.session = Session(acc, settings, client, db, engine)
        self.app_db.set_kv("current_account", acc["id"])
        self.bus.publish({"kind": "account", "current": acc["id"], "name": acc["name"]})

    async def restore(self) -> None:
        ids = [a["id"] for a in self.accounts()]
        if ids:
            last = self.app_db.get_kv("current_account")
            await self._open(last if last in ids else ids[0])

    async def activate(self, account_id: str) -> None:
        if self.app_db.get_account(account_id) is None:
            raise KeyError(account_id)
        if self.running:
            self._goto = account_id
            self._wake.set()
        else:
            await self._open(account_id)

    async def next(self) -> None:
        ids = [a["id"] for a in self.accounts()]
        if ids:
            cur = self.current_id()
            await self.activate(ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0])

    async def add(self, name: str, series: str = "") -> dict:
        acc = self.app_db.add_account(name, series)
        if self.session is None and not self.running:
            await self._open(acc["id"])
        return acc

    async def update(self, account_id: str, *, name: str | None = None,
                     series: str | None = None) -> dict | None:
        acc = self.app_db.update_account(account_id, name=name, series=series)
        if acc and self.current_id() == account_id:
            self.session.account = acc
            self.refresh_settings()
        return acc

    async def remove(self, account_id: str) -> bool:
        is_current = self.current_id() == account_id
        if is_current and self.running:
            raise ValueError("Stop the farm before deleting the active account.")
        if not self.app_db.delete_account(account_id):
            return False
        vault.delete(account_id)
        self.stats.pop(account_id, None)
        if is_current:
            await self._close_session()
            await self.restore()
        return True

    def set_cookie(self, account_id: str, session_cookie: str, extra_cookies: str = "") -> dict:
        if self.app_db.get_account(account_id) is None:
            raise KeyError(account_id)
        if not session_cookie or session_cookie.count(".") < 2:
            raise ValueError("Invalid wtcg_session cookie (a JWT is expected).")
        vault.store(account_id, "session", session_cookie)
        if extra_cookies:
            vault.store(account_id, "extra", extra_cookies)
        if self.current_id() == account_id:
            self.session.client.update_session(session_cookie, extra_cookies)
            self.session.engine.auth_error = None
            self.refresh_settings()
        status = token_status(session_cookie)
        self.bus.publish({"kind": "token", "account": account_id, **status})
        return status

    def snapshot(self) -> dict:
        self._refresh_stats()
        cur = self.current_id()
        return {
            "running": self.running,
            "current": cur,
            "cycle": self.cycle,
            "vault": {"available": vault.available(), "backend": vault.backend_name()},
            "accounts": [
                {"id": a["id"], "name": a["name"], "series": a["series"], "current": a["id"] == cur,
                 **_EMPTY_STATS, **self.stats.get(a["id"], {}),
                 "token": token_status(vault.get(a["id"], "session"))}
                for a in self.accounts()
            ],
        }

    def start(self) -> None:
        if self.running or not self.accounts():
            return
        self.running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self.running = False
        self._wake.set()
        task, self._task = self._task, None
        if task:
            await task
        if self.session and self.session.engine.running:
            await self.session.engine.stop()
        self._refresh_stats()

    async def close(self) -> None:
        await self.stop()
        await self._close_session()

    async def _pause(self, seconds: float) -> None:
        if self._goto is not None or not self.running:
            return
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _farm_current(self, multi: bool) -> tuple[bool, str]:
        s = self.session
        eng = s.engine
        opened = eng.opened_total
        eng.start()
        try:
            while self.running and eng.running and self._goto is None:
                await self._pause(self.poll)
                self._refresh_stats()
                if eng.status == "error" or (multi and eng.status == "waiting"):
                    break
        finally:
            status = eng.status
            if eng.running:
                await eng.stop()
            self._refresh_stats()
            self.stats[s.account["id"]]["status"] = (
                "error" if status == "error" else "exhausted" if status == "waiting" else "stopped")
        return eng.opened_total > opened, status

    async def _loop(self) -> None:
        idle_turns = 0
        first = True
        try:
            while self.running:
                ids = [a["id"] for a in self.accounts()]
                if not ids:
                    break
                cur = self.current_id()
                goto, self._goto = self._goto, None
                if goto in ids:
                    target = goto
                elif cur in ids:
                    target = cur if first else ids[(ids.index(cur) + 1) % len(ids)]
                else:
                    target = ids[0]
                first = False
                if cur != target:
                    await self._open(target)
                worked, status = await self._farm_current(multi=len(ids) > 1)
                if status == "error" and len(ids) == 1:
                    break
                idle_turns = 0 if worked else idle_turns + 1
                if idle_turns >= len(ids) and self._goto is None:
                    self.cycle += 1
                    idle_turns = 0
                    await self._pause(self.idle_cycle)
        except Exception:
            log.exception("Farm loop crashed.")
        finally:
            self.running = False
            self._refresh_stats()
