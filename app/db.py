"""SQLite persistence for card collection, pulls, recycling, and audit trail."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from typing import Any

from . import strategy

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    series_id     TEXT PRIMARY KEY,
    name          TEXT,
    primary_color TEXT,
    accent_color  TEXT,
    set_size      INTEGER DEFAULT 0,
    owned_hint    INTEGER DEFAULT 0,
    pulls_hint    INTEGER DEFAULT 0,
    last_synced   INTEGER
);
CREATE TABLE IF NOT EXISTS catalog (
    series_id   TEXT,
    card_id     TEXT,
    rarity      TEXT,
    card_number INTEGER,
    title       TEXT,
    PRIMARY KEY (series_id, card_id)
);
CREATE TABLE IF NOT EXISTS inventory (
    series_id    TEXT,
    card_id      TEXT,
    rarity       TEXT,
    quantity     INTEGER DEFAULT 1,
    first_pulled INTEGER,
    updated_at   INTEGER,
    PRIMARY KEY (series_id, card_id)
);
CREATE TABLE IF NOT EXISTS pull_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER,
    series_id TEXT,
    card_id   TEXT,
    rarity    TEXT,
    was_new   INTEGER
);
CREATE TABLE IF NOT EXISTS recycle_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER,
    instance_id  TEXT,
    card_id      TEXT,
    rarity       TEXT,
    ink_earned   INTEGER
);
CREATE TABLE IF NOT EXISTS recycle_failures (
    pull_id    TEXT PRIMARY KEY,
    card_id    TEXT,
    rarity     TEXT,
    fail_count INTEGER DEFAULT 1,
    retry_at   INTEGER,
    last_ts    INTEGER
);
CREATE TABLE IF NOT EXISTS actions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER,
    type      TEXT,
    series_id TEXT,
    level     TEXT,
    message   TEXT,
    detail    TEXT,
    ink_delta INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS resource_snapshots (
    ts          INTEGER PRIMARY KEY,
    ink         INTEGER,
    free_packs  INTEGER,
    paid_packs  INTEGER,
    level       INTEGER,
    xp          INTEGER
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS accounts (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    series   TEXT NOT NULL DEFAULT '',
    db_path  TEXT NOT NULL,
    position INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(ts DESC);
CREATE INDEX IF NOT EXISTS idx_pull_series ON pull_log(series_id);
"""

_SERIES_ID = re.compile(r"[A-Za-z0-9_-]{0,64}")


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "account"


def now_ms() -> int:
    return int(time.time() * 1000)


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            # lightweight migrations for already-created databases
            for col in ("owned_hint INTEGER DEFAULT 0", "pulls_hint INTEGER DEFAULT 0"):
                try:
                    self._conn.execute(f"ALTER TABLE series ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _scalar(self, sql: str, params: tuple = (), default: Any = 0) -> Any:
        rows = self._query(sql, params)
        return rows[0][0] if rows else default

    def list_accounts(self) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT id, name, series, db_path FROM accounts ORDER BY position")]

    def get_account(self, account_id: str) -> dict | None:
        rows = self._query("SELECT id, name, series, db_path FROM accounts WHERE id=?", (account_id,))
        return dict(rows[0]) if rows else None

    def _check_account(self, name: str, series: str, account_id: str = "") -> tuple[str, str]:
        name, series = (name or "").strip(), (series or "").strip()
        if not 1 <= len(name) <= 40:
            raise ValueError("Account name must be 1-40 characters.")
        if not _SERIES_ID.fullmatch(series):
            raise ValueError("Series id may only contain letters, digits, '-' and '_'.")
        if self._scalar("SELECT COUNT(*) FROM accounts WHERE name=? AND id<>?", (name, account_id)):
            raise ValueError(f"Account name already used: {name}")
        return name, series

    def add_account(self, name: str, series: str = "") -> dict:
        name, series = self._check_account(name, series)
        base = account_id = _slug(name)
        n = 1
        while self.get_account(account_id):
            n += 1
            account_id = f"{base}-{n}"
        self._exec("INSERT INTO accounts(id, name, series, db_path, position) "
                   "VALUES (?,?,?,?,(SELECT COALESCE(MAX(position), 0) + 1 FROM accounts))",
                   (account_id, name, series, f"wikitcg_{account_id}.db"))
        return self.get_account(account_id)

    def update_account(self, account_id: str, *, name: str | None = None,
                       series: str | None = None) -> dict | None:
        acc = self.get_account(account_id)
        if acc is None:
            return None
        name, series = self._check_account(acc["name"] if name is None else name,
                                           acc["series"] if series is None else series, account_id)
        self._exec("UPDATE accounts SET name=?, series=? WHERE id=?", (name, series, account_id))
        return self.get_account(account_id)

    def delete_account(self, account_id: str) -> bool:
        return self._exec("DELETE FROM accounts WHERE id=?", (account_id,)).rowcount > 0

    def upsert_series(self, sid: str, name: str, primary: str, accent: str, set_size: int) -> None:
        self._exec(
            """INSERT INTO series(series_id,name,primary_color,accent_color,set_size,last_synced)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(series_id) DO UPDATE SET
                 name=excluded.name, primary_color=excluded.primary_color,
                 accent_color=excluded.accent_color,
                 set_size=CASE WHEN excluded.set_size > 0 THEN excluded.set_size
                               ELSE series.set_size END,
                 last_synced=excluded.last_synced""",
            (sid, name, primary, accent, set_size, now_ms()),
        )

    def series_sizes(self) -> dict[str, int]:
        return {r["series_id"]: r["set_size"] or 0
                for r in self._query("SELECT series_id, set_size FROM series")}

    def set_collection_hint(self, sid: str, owned: int | None, pulls: int | None) -> None:
        self._exec("UPDATE series SET owned_hint=?, pulls_hint=? WHERE series_id=?",
                   (owned or 0, pulls or 0, sid))

    def replace_catalog(self, sid: str, cards: list[dict]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM catalog WHERE series_id=?", (sid,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO catalog(series_id,card_id,rarity,card_number,title)"
                " VALUES(?,?,?,?,?)",
                [(sid, c["id"], c.get("rarity"), c.get("cardNumber"), c.get("title")) for c in cards],
            )
            self._conn.commit()

    def catalog_size(self, sid: str) -> int:
        return self._scalar("SELECT COUNT(*) FROM catalog WHERE series_id=?", (sid,))

    def known_series_ids(self) -> list[str]:
        return [r["series_id"] for r in self._query("SELECT series_id FROM series")]

    def replace_inventory(self, sid: str, items: list[dict]) -> None:
        ts = now_ms()
        with self._lock:
            self._conn.execute("DELETE FROM inventory WHERE series_id=?", (sid,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO inventory"
                "(series_id,card_id,rarity,quantity,first_pulled,updated_at) VALUES(?,?,?,?,?,?)",
                [(sid, it["card_id"], it.get("rarity"), it.get("quantity", 1),
                  it.get("first_pulled"), ts) for it in items],
            )
            self._conn.commit()

    def bump_inventory(self, sid: str, card_id: str, rarity: str) -> bool:
        """Increment (or create) a card. Returns True if it was a NEW card."""
        ts = now_ms()
        with self._lock:
            row = self._conn.execute(
                "SELECT quantity FROM inventory WHERE series_id=? AND card_id=?", (sid, card_id)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO inventory(series_id,card_id,rarity,quantity,first_pulled,updated_at)"
                    " VALUES(?,?,?,1,?,?)", (sid, card_id, rarity, ts, ts))
                is_new = True
            else:
                self._conn.execute(
                    "UPDATE inventory SET quantity=quantity+1, updated_at=? "
                    "WHERE series_id=? AND card_id=?", (ts, sid, card_id))
                is_new = False
            self._conn.commit()
            return is_new

    def owned_count(self, sid: str) -> int:
        """Number of distinct card types with quantity >= 1."""
        return self._scalar("SELECT COUNT(*) FROM inventory WHERE series_id=? AND quantity>0", (sid,))

    def duplicates(self, sid: str) -> list[sqlite3.Row]:
        """Cards of the series with quantity > 1."""
        return self._query(
            "SELECT card_id, rarity, quantity FROM inventory WHERE series_id=? AND quantity>1",
            (sid,))

    def decrement_inventory(self, sid: str, card_id: str, by: int = 1) -> None:
        """Decrease card quantity by N, floor to 0."""
        self._exec("UPDATE inventory SET quantity=MAX(quantity-?,0), updated_at=? "
                   "WHERE series_id=? AND card_id=?", (by, now_ms(), sid, card_id))

    def missing_cards(self, sid: str) -> list[dict]:
        """Catalog cards not owned (quantity=0 or absent from inventory)."""
        rows = self._query(
            "SELECT c.card_id, c.rarity, c.title, c.card_number FROM catalog c "
            "LEFT JOIN inventory i ON i.series_id=c.series_id AND i.card_id=c.card_id AND i.quantity>0 "
            "WHERE c.series_id=? AND i.card_id IS NULL", (sid,))
        return [{"card_id": r["card_id"], "rarity": r["rarity"],
                 "title": r["title"], "card_number": r["card_number"]} for r in rows]

    def missing_card_ids(self) -> set[str]:
        """All catalog card_ids not owned (quantity=0 or absent), across all series."""
        return {r["card_id"] for r in self._query(
            "SELECT c.card_id FROM catalog c "
            "LEFT JOIN inventory i ON i.series_id=c.series_id AND i.card_id=c.card_id AND i.quantity>0 "
            "WHERE i.card_id IS NULL")}

    def missing_cards_grouped(self) -> dict[str, list[dict]]:
        """Missing cards (not owned) grouped by series, all series in single query."""
        out: dict[str, list[dict]] = {}
        for r in self._query(
                "SELECT c.series_id, c.card_id, c.rarity, c.title, c.card_number FROM catalog c "
                "LEFT JOIN inventory i ON i.series_id=c.series_id AND i.card_id=c.card_id AND i.quantity>0 "
                "WHERE i.card_id IS NULL"):
            out.setdefault(r["series_id"], []).append(
                {"card_id": r["card_id"], "rarity": r["rarity"],
                 "title": r["title"], "card_number": r["card_number"]})
        return out

    def missing_by_rarity(self) -> dict[str, int]:
        """Count of not-owned cards per rarity across all series (quantity=0 or absent).
        Used by reserve mode: keep this many of each rarity as backups."""
        rows = self._query(
            "SELECT c.rarity AS rarity, COUNT(*) AS n FROM catalog c "
            "LEFT JOIN inventory i ON i.series_id=c.series_id AND i.card_id=c.card_id AND i.quantity>0 "
            "WHERE i.card_id IS NULL GROUP BY c.rarity")
        return {r["rarity"]: r["n"] for r in rows if r["rarity"]}

    def rarity_breakdown(self, sid: str) -> list[dict]:
        """Owned (quantity>0) vs total of the set, per rarity (rarest to most common)."""
        cat = self._query("SELECT rarity, COUNT(*) n FROM catalog WHERE series_id=? GROUP BY rarity", (sid,))
        own = self._query(
            "SELECT i.rarity, COUNT(*) n FROM inventory i "
            "JOIN catalog c ON c.series_id=i.series_id AND c.card_id=i.card_id "
            "WHERE i.series_id=? AND i.quantity>0 GROUP BY i.rarity", (sid,))
        totals = {r["rarity"]: r["n"] for r in cat}
        owned = {r["rarity"]: r["n"] for r in own}
        return [{"rarity": r, "owned": owned.get(r, 0), "total": totals.get(r, 0)}
                for r in strategy.RARITY_ORDER_DESC if totals.get(r, 0)]

    def all_duplicates(self) -> list[dict]:
        """All cards with quantity>1 across all series."""
        rows = self._query(
            "SELECT series_id, card_id, rarity, quantity FROM inventory WHERE quantity>1")
        return [dict(r) for r in rows]

    def owns_card(self, card_id: str) -> bool:
        """True if I own at least one copy of this card type (global wiki-... id)."""
        rows = self._query(
            "SELECT 1 FROM inventory WHERE card_id=? AND quantity>0 LIMIT 1", (card_id,))
        return bool(rows)

    def log_pull(self, sid: str, card_id: str, rarity: str, was_new: bool) -> None:
        """Record a pulled card in pull_log."""
        self._exec("INSERT INTO pull_log(ts,series_id,card_id,rarity,was_new) VALUES(?,?,?,?,?)",
                   (now_ms(), sid, card_id, rarity, int(was_new)))

    def log_recycle(self, instance_id: str, card_id: str, rarity: str, ink: int) -> None:
        """Record a recycled card in recycle_log."""
        self._exec("INSERT INTO recycle_log(ts,instance_id,card_id,rarity,ink_earned) "
                   "VALUES(?,?,?,?,?)", (now_ms(), instance_id, card_id, rarity, ink))

    def mark_recycle_failure(self, pull_id: str, card_id: str, rarity: str,
                             base_seconds: float, ts: float, max_mult: int = 8) -> float:
        """Persistently remember a non-recyclable copy, with a GROWING retry delay based on
        the failure count: retry_at = ts + base_seconds * min(fail_count, max_mult).
        A persistently locked card is thus retried less and less often.
        Returns the computed `retry_at` (epoch s). `base_seconds`/`ts` are in SECONDS."""
        row = self._query("SELECT fail_count FROM recycle_failures WHERE pull_id=?", (pull_id,))
        fc = (row[0]["fail_count"] + 1) if row else 1
        retry_at = int(ts + base_seconds * min(fc, max_mult))
        self._exec(
            "INSERT INTO recycle_failures(pull_id,card_id,rarity,fail_count,retry_at,last_ts) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(pull_id) DO UPDATE SET fail_count=?, retry_at=?, last_ts=?, "
            "rarity=?, card_id=?",
            (pull_id, card_id, rarity, fc, retry_at, int(ts),
             fc, retry_at, int(ts), rarity, card_id))
        return retry_at

    def recycle_skips(self) -> dict[str, float]:
        """{pull_id: retry_at} — loaded at startup so we don't retry before the due time."""
        return {r["pull_id"]: float(r["retry_at"] or 0)
                for r in self._query("SELECT pull_id, retry_at FROM recycle_failures")}

    def clear_recycle_failure(self, pull_id: str) -> None:
        """Forget about a locked card after successful recycle."""
        self._exec("DELETE FROM recycle_failures WHERE pull_id=?", (pull_id,))

    def recycle_failures_summary(self) -> dict:
        """Count of locked cards, grouped by rarity, and timestamp of next retry."""
        total = self._scalar("SELECT COUNT(*) FROM recycle_failures")
        by = [{"rarity": r["rarity"], "count": r["n"]} for r in self._query(
            "SELECT rarity, COUNT(*) n FROM recycle_failures GROUP BY rarity")]
        nxt = self._query("SELECT MIN(retry_at) m FROM recycle_failures")
        return {"total": total, "by_rarity": by,
                "next_retry_at": (nxt[0]["m"] if nxt and nxt[0]["m"] else None)}

    def log_action(self, type_: str, message: str, *, series_id: str = "",
                   level: str = "info", detail: dict | None = None, ink_delta: int = 0) -> dict:
        """Record an action (event) with optional structured data."""
        ts = now_ms()
        self._exec(
            "INSERT INTO actions(ts,type,series_id,level,message,detail,ink_delta) "
            "VALUES(?,?,?,?,?,?,?)",
            (ts, type_, series_id, level, message,
             json.dumps(detail or {}, ensure_ascii=False), ink_delta))
        return {"ts": ts, "type": type_, "series_id": series_id, "level": level,
                "message": message, "detail": detail or {}, "ink_delta": ink_delta}

    def recent_actions(self, limit: int = 80) -> list[dict]:
        """Most recent N actions (newest first)."""
        rows = self._query(
            "SELECT ts,type,series_id,level,message,detail,ink_delta "
            "FROM actions ORDER BY ts DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
            except json.JSONDecodeError:
                d["detail"] = {}
            out.append(d)
        return out

    def snapshot_resources(self, ink: int, free: int, paid: int, level: int, xp: int) -> None:
        """Record current ink/packs/level for charting over time."""
        self._exec("INSERT OR REPLACE INTO resource_snapshots"
                   "(ts,ink,free_packs,paid_packs,level,xp) VALUES(?,?,?,?,?,?)",
                   (now_ms(), ink, free, paid, level, xp))

    def empirical_pull_rates(self) -> dict[str, float]:
        """Observed pull rate for each rarity (pulls of rarity / total pulls)."""
        rows = self._query("SELECT rarity, COUNT(*) AS n FROM pull_log GROUP BY rarity")
        total = sum(r["n"] for r in rows) or 1
        return {r["rarity"]: r["n"] / total for r in rows}

    def empirical_recycle_values(self) -> dict[str, float]:
        """Average ink earned per recycled card, by rarity."""
        rows = self._query(
            "SELECT rarity, AVG(ink_earned) AS v FROM recycle_log GROUP BY rarity")
        return {r["rarity"]: float(r["v"]) for r in rows if r["v"] is not None}

    def resource_history(self, limit: int = 120) -> list[dict]:
        """Ink/packs/level snapshots over time (chronological ascending order)."""
        rows = self._query(
            "SELECT ts,ink,free_packs,paid_packs,level,xp FROM resource_snapshots "
            "ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in reversed(rows)]

    def pull_counts(self) -> list[dict]:
        """Pulls per rarity: total + new cards (was_new)."""
        rows = self._query(
            "SELECT rarity, COUNT(*) n, COALESCE(SUM(was_new),0) nw FROM pull_log GROUP BY rarity")
        return [{"rarity": r["rarity"], "count": r["n"], "new": r["nw"]} for r in rows]

    def pull_rarities_by_series(self) -> dict[str, dict[str, int]]:
        """Pull counts per series, then per rarity."""
        out: dict[str, dict[str, int]] = {}
        for r in self._query("SELECT series_id, rarity, COUNT(*) n FROM pull_log GROUP BY series_id, rarity"):
            out.setdefault(r["series_id"], {})[r["rarity"]] = r["n"]
        return out

    def recycle_summary(self) -> dict:
        """Recycle summary: total copies + ink, and a per-rarity breakdown."""
        by = [{"rarity": r["rarity"], "count": r["n"], "ink": r["ink"] or 0}
              for r in self._query(
                  "SELECT rarity, COUNT(*) n, COALESCE(SUM(ink_earned),0) ink "
                  "FROM recycle_log GROUP BY rarity")]
        tot = self._query(
            "SELECT COUNT(*) n, COALESCE(SUM(ink_earned),0) ink FROM recycle_log")[0]
        return {"total_count": tot["n"], "total_ink": tot["ink"], "by_rarity": by}

    def progress_view(self) -> list[dict]:
        """Series completion progress: total cards, owned, missing, pull count, %."""
        rows = self._query("""
            SELECT s.series_id, s.name, s.primary_color, s.accent_color, s.set_size,
                   s.owned_hint, s.pulls_hint,
                   (SELECT COUNT(*) FROM catalog c   WHERE c.series_id=s.series_id) AS catalog_n,
                   (SELECT COUNT(*) FROM inventory i WHERE i.series_id=s.series_id AND i.quantity>0) AS inv_n,
                   (SELECT COALESCE(SUM(quantity),0) FROM inventory i WHERE i.series_id=s.series_id) AS inv_pulls
            FROM series s ORDER BY s.name
        """)
        out = []
        for r in rows:
            total = r["set_size"] or r["catalog_n"] or 0
            owned = r["inv_n"] if r["inv_n"] else (r["owned_hint"] or 0)
            pulls = r["inv_pulls"] if r["inv_n"] else (r["pulls_hint"] or 0)
            out.append({
                "series_id": r["series_id"], "name": r["name"] or r["series_id"],
                "primary_color": r["primary_color"], "accent_color": r["accent_color"],
                "total": total, "owned": owned, "missing": max(total - owned, 0),
                "pulls": pulls,
                "pct": round(100 * owned / total, 1) if total else 0.0,
            })
        return out

    def get_kv(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_kv(self, key: str, value: str) -> None:
        """Store a string value in the kv table."""
        self._exec("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, value))

    def get_json(self, key: str, default: Any = None) -> Any:
        """Read a JSON value from kv; returns `default` if absent or unreadable."""
        raw = self.get_kv(key)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    def set_json(self, key: str, value: Any) -> None:
        """Store a JSON-serializable value in kv."""
        self.set_kv(key, json.dumps(value))
