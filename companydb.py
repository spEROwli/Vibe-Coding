#!/usr/bin/env python3
"""
companydb.py — SQLite company/slug source for the PM scraper.

Why this exists
---------------
pmfarm.py scrapes a fixed list of ATS slugs. A flat JSON list can't remember
which slugs are actually live vs. dead, so every run wastes time re-probing 404s
and you can't grow the list intelligently. This module backs that list with a
small SQLite database that LEARNS:

  • every run records, per slug, whether it returned roles (ok), resolved empty,
    or failed (404/timeout);
  • slugs that fail repeatedly are auto-marked "dead" and dropped from future
    runs (self-cleaning) — they stay in the DB so we never re-add them blindly;
  • live slugs float to the top, ordered by how often they yield roles.

The DB (companies.db) is a derived cache: rebuild it any time from the seed with
  python3 build_companydb.py
so it is intentionally NOT committed to git. The human-readable seed
(verified_companies.json + the curated list in build_companydb.py) is the durable
source of truth that IS committed.

Public API
----------
  init_db(path)                       create schema if absent
  add_companies(rows, path)           upsert seed/discovered companies
  load_active(ats, path)              -> [slug, …] live+untried, dead excluded
  record_results(resolution, path)    fold a run's {"GH:slug":"ok"} map back in
  stats(path)                         -> dict of per-ats status counts
"""

import datetime
import sqlite3
from pathlib import Path

DB_FILE = "companies.db"

# A slug that fails (404/timeout) this many runs IN A ROW is marked dead and
# dropped from active scraping. Generous so a one-off network blip never buries
# a real board.
DEAD_AFTER_CONSECUTIVE_FAILS = 4

# Map pmfarm's _slug_resolution source prefixes → ats names used in the DB.
_SRC_TO_ATS = {"GH": "greenhouse", "Ashby": "ashby", "Lever": "lever"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    ats          TEXT NOT NULL,                 -- greenhouse | ashby | lever
    slug         TEXT NOT NULL,
    name         TEXT,
    tags         TEXT,                           -- comma-joined, informational
    source       TEXT,                           -- seed | yc | candidate | manual
    status       TEXT NOT NULL DEFAULT 'unprobed', -- live|empty|dead|unprobed
    last_checked TEXT,                            -- ISO date of last probe
    last_roles   INTEGER NOT NULL DEFAULT 0,      -- roles found on last probe
    runs         INTEGER NOT NULL DEFAULT 0,      -- total probes
    hits         INTEGER NOT NULL DEFAULT 0,      -- total probes that found roles
    cfails       INTEGER NOT NULL DEFAULT 0,      -- consecutive failures
    added        TEXT,                            -- ISO date first added
    PRIMARY KEY (ats, slug)
);
"""


def _today() -> str:
    return datetime.date.today().isoformat()


def _connect(path: str | Path = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str | Path = DB_FILE) -> None:
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)


def add_companies(rows: list[dict], path: str | Path = DB_FILE) -> int:
    """Upsert seed/discovered companies. Each row: {ats, slug, name?, tags?, source?}.
    Existing rows keep their learned stats; only name/tags/source are refreshed.
    Returns the number of NEW companies inserted."""
    init_db(path)
    inserted = 0
    with _connect(path) as conn:
        for r in rows:
            ats  = (r.get("ats")  or "").strip().lower()
            slug = (r.get("slug") or "").strip()
            if not ats or not slug:
                continue
            tags = r.get("tags") or ""
            if isinstance(tags, (list, tuple)):
                tags = ",".join(tags)
            cur = conn.execute(
                "SELECT 1 FROM companies WHERE ats=? AND slug=?", (ats, slug))
            if cur.fetchone():
                conn.execute(
                    "UPDATE companies SET name=COALESCE(?,name), "
                    "tags=COALESCE(NULLIF(?,''),tags), "
                    "source=COALESCE(NULLIF(?,''),source) WHERE ats=? AND slug=?",
                    (r.get("name"), tags, r.get("source"), ats, slug))
            else:
                conn.execute(
                    "INSERT INTO companies (ats, slug, name, tags, source, added) "
                    "VALUES (?,?,?,?,?,?)",
                    (ats, slug, r.get("name"), tags,
                     r.get("source") or "seed", _today()))
                inserted += 1
    return inserted


def load_active(ats: str, path: str | Path = DB_FILE) -> list[str]:
    """Return slugs worth probing for one ATS, best-first. Dead slugs excluded.
    Order: proven live (by hit count) → resolved-empty → never-probed."""
    if not Path(path).exists():
        return []
    ats = ats.strip().lower()
    with _connect(path) as conn:
        cur = conn.execute(
            "SELECT slug FROM companies WHERE ats=? AND status!='dead' "
            "ORDER BY CASE status WHEN 'live' THEN 0 WHEN 'empty' THEN 1 "
            "  WHEN 'unprobed' THEN 2 ELSE 3 END, hits DESC, slug ASC",
            (ats,))
        return [row["slug"] for row in cur.fetchall()]


def record_results(resolution: dict[str, str], path: str | Path = DB_FILE) -> None:
    """Fold one run's slug-resolution map back into the DB to update hit-rates.

    `resolution` is pmfarm._slug_resolution: keys "GH:slug"/"Ashby:slug"/
    "Lever:slug", values "ok" | "empty" | "fail". Slugs not yet in the DB are
    inserted (so manually-added fallback slugs start getting tracked)."""
    if not resolution:
        return
    init_db(path)
    today = _today()
    with _connect(path) as conn:
        for key, status in resolution.items():
            src, _, slug = key.partition(":")
            ats = _SRC_TO_ATS.get(src)
            if not ats or not slug:
                continue
            row = conn.execute(
                "SELECT status, runs, hits, cfails FROM companies "
                "WHERE ats=? AND slug=?", (ats, slug)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO companies (ats, slug, source, added) VALUES (?,?,?,?)",
                    (ats, slug, "auto", today))
                runs = hits = cfails = 0
            else:
                runs, hits, cfails = row["runs"], row["hits"], row["cfails"]

            runs += 1
            if status == "ok":
                hits += 1; cfails = 0; new_status = "live"; last_roles = 1
            elif status == "empty":
                cfails = 0; new_status = "empty"; last_roles = 0
            else:  # fail
                cfails += 1; last_roles = 0
                new_status = "dead" if cfails >= DEAD_AFTER_CONSECUTIVE_FAILS else \
                             ("live" if hits else "unprobed")
            conn.execute(
                "UPDATE companies SET status=?, runs=?, hits=?, cfails=?, "
                "last_checked=?, last_roles=? WHERE ats=? AND slug=?",
                (new_status, runs, hits, cfails, today, last_roles, ats, slug))


def stats(path: str | Path = DB_FILE) -> dict:
    """Return {ats: {status: count}} plus a 'total' row, for reporting."""
    out: dict = {}
    if not Path(path).exists():
        return out
    with _connect(path) as conn:
        for row in conn.execute(
                "SELECT ats, status, COUNT(*) n FROM companies GROUP BY ats, status"):
            out.setdefault(row["ats"], {})[row["status"]] = row["n"]
        total = conn.execute("SELECT COUNT(*) n FROM companies").fetchone()["n"]
    out["_total"] = total
    return out


if __name__ == "__main__":
    # Quick status report: python3 companydb.py
    s = stats()
    if not s:
        print(f"No {DB_FILE} yet. Build it with: python3 build_companydb.py")
    else:
        print(f"{DB_FILE}: {s.pop('_total')} companies")
        for ats, counts in sorted(s.items()):
            line = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            print(f"  {ats:11s} {line}")
