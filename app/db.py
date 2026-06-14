"""Persistance SQLite.

Tables :
  series              catalogue des séries (couleurs, taille du set)
  catalog             toutes les cartes d'une série (le « set » complet)
  inventory           mes cartes (type + quantité) — base du calcul de complétion
  pull_log            chaque carte tirée (pour apprendre les taux empiriquement)
  recycle_log         chaque recyclage réussi (pour apprendre la valeur d'encre par rareté)
  recycle_failures    exemplaires non recyclables (500) + heure de réessai (persistant)
  actions             historique horodaté de toutes les actions (UI / audit)
  resource_snapshots  encre / packs / niveau dans le temps
  kv                  divers (clé/valeur)

La complétion d'une série = nb de lignes inventory(series) / catalog count(series).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

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
-- Mémoire des exemplaires NON recyclables (500) : verrouillés côté serveur d'une manière non
-- exposée par l'API. Persistée pour ne pas re-tenter à chaque redémarrage avant `retry_at`.
CREATE TABLE IF NOT EXISTS recycle_failures (
    pull_id    TEXT PRIMARY KEY,
    card_id    TEXT,
    rarity     TEXT,
    fail_count INTEGER DEFAULT 1,
    retry_at   INTEGER,      -- epoch (s) du prochain essai autorisé
    last_ts    INTEGER       -- epoch (s) du dernier échec
);
CREATE TABLE IF NOT EXISTS actions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER,
    type      TEXT,
    series_id TEXT,
    level     TEXT,      -- info | warn | error
    message   TEXT,
    detail    TEXT,      -- JSON
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
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(ts DESC);
CREATE INDEX IF NOT EXISTS idx_pull_series ON pull_log(series_id);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            # migrations légères pour les BDD déjà créées
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

    # ---------- helpers ----------
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---------- catalogue & séries ----------
    def upsert_series(self, sid: str, name: str, primary: str, accent: str, set_size: int) -> None:
        self._exec(
            """INSERT INTO series(series_id,name,primary_color,accent_color,set_size,last_synced)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(series_id) DO UPDATE SET
                 name=excluded.name, primary_color=excluded.primary_color,
                 accent_color=excluded.accent_color, set_size=excluded.set_size,
                 last_synced=excluded.last_synced""",
            (sid, name, primary, accent, set_size, now_ms()),
        )

    def set_collection_hint(self, sid: str, owned: int | None, pulls: int | None) -> None:
        """Stocke owned/total_pulls issus de /api/collection pour afficher la
        progression immédiatement, avant le chargement du détail (inventaire)."""
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
        rows = self._query("SELECT COUNT(*) AS n FROM catalog WHERE series_id=?", (sid,))
        return rows[0]["n"] if rows else 0

    def known_series_ids(self) -> list[str]:
        return [r["series_id"] for r in self._query("SELECT series_id FROM series")]

    # ---------- inventaire ----------
    def replace_inventory(self, sid: str, items: list[dict]) -> None:
        """items: [{card_id, rarity, quantity, first_pulled}]"""
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
        """Incrémente (ou crée) une carte. Renvoie True si c'était une NOUVELLE carte."""
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
        rows = self._query("SELECT COUNT(*) AS n FROM inventory WHERE series_id=?", (sid,))
        return rows[0]["n"] if rows else 0

    def duplicates(self, sid: str) -> list[sqlite3.Row]:
        """Cartes de la série avec quantity > 1."""
        return self._query(
            "SELECT card_id, rarity, quantity FROM inventory WHERE series_id=? AND quantity>1",
            (sid,))

    def decrement_inventory(self, sid: str, card_id: str, by: int = 1) -> None:
        self._exec("UPDATE inventory SET quantity=MAX(quantity-?,0), updated_at=? "
                   "WHERE series_id=? AND card_id=?", (by, now_ms(), sid, card_id))

    def missing_cards(self, sid: str) -> list[dict]:
        """Cartes du catalogue de la série que je ne possède PAS encore."""
        rows = self._query(
            "SELECT c.card_id, c.rarity, c.title, c.card_number FROM catalog c "
            "LEFT JOIN inventory i ON i.series_id=c.series_id AND i.card_id=c.card_id "
            "WHERE c.series_id=? AND i.card_id IS NULL", (sid,))
        return [{"card_id": r["card_id"], "rarity": r["rarity"],
                 "title": r["title"], "card_number": r["card_number"]} for r in rows]

    def missing_by_rarity(self) -> dict[str, int]:
        """Nombre de cartes MANQUANTES par rareté, toutes séries confondues (catalogue - inventaire).
        Sert au recyclage « réserve = nb de manquantes par rareté »."""
        rows = self._query(
            "SELECT c.rarity AS rarity, COUNT(*) AS n FROM catalog c "
            "LEFT JOIN inventory i ON i.series_id=c.series_id AND i.card_id=c.card_id "
            "WHERE i.card_id IS NULL GROUP BY c.rarity")
        return {r["rarity"]: r["n"] for r in rows if r["rarity"]}

    def rarity_breakdown(self, sid: str) -> list[dict]:
        """Possédées vs total du set, par rareté (du plus rare au plus commun)."""
        cat = self._query("SELECT rarity, COUNT(*) n FROM catalog WHERE series_id=? GROUP BY rarity", (sid,))
        own = self._query(
            "SELECT i.rarity, COUNT(*) n FROM inventory i "
            "JOIN catalog c ON c.series_id=i.series_id AND c.card_id=i.card_id "
            "WHERE i.series_id=? GROUP BY i.rarity", (sid,))
        totals = {r["rarity"]: r["n"] for r in cat}
        owned = {r["rarity"]: r["n"] for r in own}
        order = ["LR", "UR", "SSR", "SR", "R", "UC", "C"]
        return [{"rarity": r, "owned": owned.get(r, 0), "total": totals.get(r, 0)}
                for r in order if totals.get(r, 0)]

    def all_duplicates(self) -> list[dict]:
        """Toutes mes cartes en plusieurs exemplaires (toutes séries)."""
        rows = self._query(
            "SELECT series_id, card_id, rarity, quantity FROM inventory WHERE quantity>1")
        return [dict(r) for r in rows]

    def owns_card(self, card_id: str) -> bool:
        """Vrai si je possède au moins un exemplaire de ce type de carte (id global wiki-…)."""
        rows = self._query(
            "SELECT 1 FROM inventory WHERE card_id=? AND quantity>0 LIMIT 1", (card_id,))
        return bool(rows)

    # ---------- logs ----------
    def log_pull(self, sid: str, card_id: str, rarity: str, was_new: bool) -> None:
        self._exec("INSERT INTO pull_log(ts,series_id,card_id,rarity,was_new) VALUES(?,?,?,?,?)",
                   (now_ms(), sid, card_id, rarity, int(was_new)))

    def log_recycle(self, instance_id: str, card_id: str, rarity: str, ink: int) -> None:
        self._exec("INSERT INTO recycle_log(ts,instance_id,card_id,rarity,ink_earned) "
                   "VALUES(?,?,?,?,?)", (now_ms(), instance_id, card_id, rarity, ink))

    # ---------- mémoire des échecs de recyclage (exemplaires 500) ----------
    def mark_recycle_failure(self, pull_id: str, card_id: str, rarity: str,
                             base_seconds: float, ts: float, max_mult: int = 8) -> float:
        """Mémorise (persistant) un exemplaire non recyclable, avec un délai de réessai
        CROISSANT selon le nombre d'échecs : retry_at = ts + base_seconds × min(fail_count, max_mult).
        Une carte durablement verrouillée est ainsi re-tentée de moins en moins souvent.
        Renvoie le `retry_at` (epoch s) calculé. `base_seconds`/`ts` en SECONDES."""
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
        """{pull_id: retry_at} — chargé au démarrage pour ne pas re-tenter avant l'heure."""
        return {r["pull_id"]: float(r["retry_at"] or 0)
                for r in self._query("SELECT pull_id, retry_at FROM recycle_failures")}

    def clear_recycle_failure(self, pull_id: str) -> None:
        self._exec("DELETE FROM recycle_failures WHERE pull_id=?", (pull_id,))

    def recycle_failures_summary(self) -> dict:
        total = self._query("SELECT COUNT(*) n FROM recycle_failures")[0]["n"]
        by = [{"rarity": r["rarity"], "count": r["n"]} for r in self._query(
            "SELECT rarity, COUNT(*) n FROM recycle_failures GROUP BY rarity")]
        nxt = self._query("SELECT MIN(retry_at) m FROM recycle_failures")
        return {"total": total, "by_rarity": by,
                "next_retry_at": (nxt[0]["m"] if nxt and nxt[0]["m"] else None)}

    def log_action(self, type_: str, message: str, *, series_id: str = "",
                   level: str = "info", detail: dict | None = None, ink_delta: int = 0) -> dict:
        ts = now_ms()
        self._exec(
            "INSERT INTO actions(ts,type,series_id,level,message,detail,ink_delta) "
            "VALUES(?,?,?,?,?,?,?)",
            (ts, type_, series_id, level, message,
             json.dumps(detail or {}, ensure_ascii=False), ink_delta))
        return {"ts": ts, "type": type_, "series_id": series_id, "level": level,
                "message": message, "detail": detail or {}, "ink_delta": ink_delta}

    def recent_actions(self, limit: int = 80) -> list[dict]:
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
        self._exec("INSERT OR REPLACE INTO resource_snapshots"
                   "(ts,ink,free_packs,paid_packs,level,xp) VALUES(?,?,?,?,?,?)",
                   (now_ms(), ink, free, paid, level, xp))

    # ---------- analytics : taux & valeurs appris empiriquement ----------
    def empirical_pull_rates(self) -> dict[str, float]:
        rows = self._query("SELECT rarity, COUNT(*) AS n FROM pull_log GROUP BY rarity")
        total = sum(r["n"] for r in rows) or 1
        return {r["rarity"]: r["n"] / total for r in rows}

    def empirical_recycle_values(self) -> dict[str, float]:
        rows = self._query(
            "SELECT rarity, AVG(ink_earned) AS v FROM recycle_log GROUP BY rarity")
        return {r["rarity"]: float(r["v"]) for r in rows if r["v"] is not None}

    # ---------- stats pour les graphiques de l'UI ----------
    def resource_history(self, limit: int = 120) -> list[dict]:
        """Snapshots encre/packs/niveau dans le temps (ordre chronologique croissant)."""
        rows = self._query(
            "SELECT ts,ink,free_packs,paid_packs,level,xp FROM resource_snapshots "
            "ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in reversed(rows)]

    def pull_counts(self) -> list[dict]:
        """Tirages par rareté : total + nouvelles cartes (was_new)."""
        rows = self._query(
            "SELECT rarity, COUNT(*) n, COALESCE(SUM(was_new),0) nw FROM pull_log GROUP BY rarity")
        return [{"rarity": r["rarity"], "count": r["n"], "new": r["nw"]} for r in rows]

    def recycle_summary(self) -> dict:
        """Bilan de recyclage : total exemplaires + encre, et détail par rareté."""
        by = [{"rarity": r["rarity"], "count": r["n"], "ink": r["ink"] or 0}
              for r in self._query(
                  "SELECT rarity, COUNT(*) n, COALESCE(SUM(ink_earned),0) ink "
                  "FROM recycle_log GROUP BY rarity")]
        tot = self._query(
            "SELECT COUNT(*) n, COALESCE(SUM(ink_earned),0) ink FROM recycle_log")[0]
        return {"total_count": tot["n"], "total_ink": tot["ink"], "by_rarity": by}

    # ---------- état complet pour l'UI ----------
    def progress_view(self) -> list[dict]:
        rows = self._query("""
            SELECT s.series_id, s.name, s.primary_color, s.accent_color, s.set_size,
                   s.owned_hint, s.pulls_hint,
                   (SELECT COUNT(*) FROM catalog c   WHERE c.series_id=s.series_id) AS catalog_n,
                   (SELECT COUNT(*) FROM inventory i WHERE i.series_id=s.series_id) AS inv_n,
                   (SELECT COALESCE(SUM(quantity),0) FROM inventory i WHERE i.series_id=s.series_id) AS inv_pulls
            FROM series s ORDER BY s.name
        """)
        out = []
        for r in rows:
            total = r["set_size"] or r["catalog_n"] or 0
            # détail chargé -> chiffres exacts ; sinon -> indice de /api/collection
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
        self._exec("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, value))
