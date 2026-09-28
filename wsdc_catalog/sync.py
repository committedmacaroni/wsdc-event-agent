"""WSDC calendar synchronization: parse -> guard -> upsert -> deactivate -> record."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from .db import iso, new_id, utcnow
from .fetcher import FetchResult
from .normalize import is_short_alias, normalize_text
from .parser import ParsedEvent, parse_event_list

SOURCE = "wsdc_calendar"
TRACKED_FIELDS = ("name", "normalized_name", "start_date", "end_date", "year", "city",
                  "region", "country", "country_raw", "location_raw", "event_status",
                  "confirmation_status", "hiatus", "website_url")
FUZZY_MATCH_DAYS = 21
GUARD_RATIO = 0.7
GUARD_MIN_BASELINE = 20
STALE_RUN_AFTER = timedelta(hours=2)
SNAPSHOTS_KEPT = 14
MAX_CHANGE_LOG = 500


class SyncAlreadyRunning(Exception):
    pass


class GuardTripped(Exception):
    pass


# ------------------------------------------------------------------ run bookkeeping
def _start_run(conn: sqlite3.Connection, source_url: str, now: datetime) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        stale_before = iso(now - STALE_RUN_AFTER)
        conn.execute(
            "UPDATE sync_runs SET status='failed', completed_at=?, "
            "errors=json_insert(errors,'$[#]','marked stale: run never completed') "
            "WHERE source=? AND status='running' AND started_at < ?",
            (iso(now), SOURCE, stale_before))
        running = conn.execute(
            "SELECT id FROM sync_runs WHERE source=? AND status='running'", (SOURCE,)).fetchone()
        if running:
            raise SyncAlreadyRunning(f"sync run {running['id']} is still running")
        cur = conn.execute(
            "INSERT INTO sync_runs (source, source_url, started_at, status) VALUES (?,?,?, 'running')",
            (SOURCE, source_url, iso(now)))
        conn.execute("COMMIT")
        return cur.lastrowid
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _finish_run(conn, run_id, status, stats, errors, warnings, changes, snapshot_path, now):
    conn.execute(
        "UPDATE sync_runs SET status=?, completed_at=?, records_found=?, records_added=?, "
        "records_updated=?, records_unchanged=?, records_deactivated=?, records_skipped=?, "
        "errors=?, warnings=?, changes=?, snapshot_path=? WHERE id=?",
        (status, iso(now), stats["found"], stats["added"], stats["updated"], stats["unchanged"],
         stats["deactivated"], stats["skipped"], json.dumps(errors), json.dumps(warnings),
         json.dumps(changes[:MAX_CHANGE_LOG]), snapshot_path, run_id))


def run_summary(conn, run_id: int) -> dict:
    r = conn.execute("SELECT * FROM sync_runs WHERE id=?", (run_id,)).fetchone()
    out = dict(r)
    for k in ("errors", "warnings", "changes"):
        out[k] = json.loads(out[k] or "[]")
    return out


def _save_snapshot(snapshot_dir, run_id, fetched: FetchResult) -> str | None:
    if not snapshot_dir:
        return None
    d = Path(snapshot_dir)
    d.mkdir(parents=True, exist_ok=True)
    main = d / f"run_{run_id:06d}_main.html"
    main.write_text(fetched.main_html, encoding="utf-8")
    if fetched.confirmed_html is not None:
        (d / f"run_{run_id:06d}_confirmed.html").write_text(fetched.confirmed_html, encoding="utf-8")
    runs = sorted({p.name.split("_")[1] for p in d.glob("run_*_*.html")})
    for old in runs[:-SNAPSHOTS_KEPT]:
        for p in d.glob(f"run_{old}_*.html"):
            p.unlink(missing_ok=True)
    return str(main)


def _check_guard(conn, found: int, force: bool, ratio: float) -> None:
    if force:
        return
    if found == 0:
        raise GuardTripped("0 events parsed; refusing to sync (use force to override)")
    prev = conn.execute(
        "SELECT records_found FROM sync_runs WHERE source=? AND status='success' "
        "ORDER BY id DESC LIMIT 1", (SOURCE,)).fetchone()
    if prev and prev[0] >= GUARD_MIN_BASELINE and found < prev[0] * ratio:
        raise GuardTripped(
            f"parsed {found} events but the last successful run found {prev[0]}; "
            f"below the {ratio:.0%} safety threshold (use force to override)")


# ------------------------------------------------------------------ series resolution
def find_series(conn, canonical_name: str, extra_names: list[str] = (), warnings=None) -> str | None:
    """Canonical normalized name first, then long (non-short) aliases."""
    norm = normalize_text(canonical_name)
    r = conn.execute("SELECT id FROM event_series WHERE normalized_name=? ORDER BY created_at, id LIMIT 1",
                     (norm,)).fetchone()
    if r:
        return r["id"]
    keys = [k for k in dict.fromkeys([norm, *(normalize_text(n) for n in extra_names)])
            if k and not is_short_alias(k)]
    if not keys:
        return None
    ph = ",".join("?" * len(keys))
    hits = conn.execute(
        f"SELECT DISTINCT a.series_id, s.created_at FROM event_aliases a "
        f"JOIN event_series s ON s.id=a.series_id WHERE a.normalized_alias IN ({ph}) "
        f"ORDER BY s.created_at, a.series_id", keys).fetchall()
    if len(hits) > 1 and warnings is not None:
        warnings.append(f"name {canonical_name!r} matches aliases of {len(hits)} series; "
                        f"using oldest {hits[0]['series_id']}")
    return hits[0]["series_id"] if hits else None


def create_series(conn, canonical_name: str, source: str, ts: str) -> str:
    sid = new_id("ser")
    conn.execute(
        "INSERT INTO event_series (id, canonical_name, normalized_name, source, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)", (sid, canonical_name, normalize_text(canonical_name), source, ts, ts))
    return sid


def add_alias(conn, series_id: str, alias: str, source: str, ts: str) -> bool:
    norm = normalize_text(alias)
    if not norm:
        return False
    series = conn.execute("SELECT normalized_name FROM event_series WHERE id=?", (series_id,)).fetchone()
    if series and series["normalized_name"] == norm:
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO event_aliases (id, series_id, alias, normalized_alias, source, created_at) "
        "VALUES (?,?,?,?,?,?)", (new_id("als"), series_id, alias, norm, source, ts))
    return cur.rowcount > 0


def resolve_or_create_series(conn, canonical: str, aliases: list[str], source: str, ts: str,
                             warnings: list[str]) -> str:
    sid = find_series(conn, canonical, aliases, warnings)
    if sid is None:
        sid = create_series(conn, canonical, source, ts)
    for a in aliases:
        add_alias(conn, sid, a, source, ts)
    return sid


# ------------------------------------------------------------------ occurrence matching
def _countries_compatible(a: str | None, b: str | None) -> bool:
    return a is None or b is None or a == b


def match_occurrence(conn, series_id: str, start: date, country: str | None, claimed: set[str],
                     warnings: list[str], *, source: str | None = SOURCE,
                     window_days: int = FUZZY_MATCH_DAYS):
    """Exact (same start date) first, then closest start within window_days."""
    sql = "SELECT * FROM events WHERE series_id=?"
    args: list = [series_id]
    if source:
        sql += " AND source=?"
        args.append(source)
    cands = [r for r in conn.execute(sql + " ORDER BY created_at, id", args)
             if r["id"] not in claimed and r["start_date"]
             and _countries_compatible(r["country"], country)]
    exact = [r for r in cands if r["start_date"] == start.isoformat()]
    if exact:
        return exact[0]
    scored = sorted(
        ((abs((date.fromisoformat(r["start_date"]) - start).days), i, r) for i, r in enumerate(cands)),
        key=lambda t: (t[0], t[1]))
    scored = [t for t in scored if t[0] <= window_days]
    if not scored:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        warnings.append(f"ambiguous date match for series {series_id} near {start}; "
                        f"picked {scored[0][2]['id']}")
    return scored[0][2]


# ------------------------------------------------------------------ row application
def _fields_for(row: ParsedEvent, confirmation: str) -> dict:
    return {
        "name": row.name, "normalized_name": row.normalized_name,
        "start_date": row.start_date.isoformat(), "end_date": row.end_date.isoformat(),
        "year": row.start_date.year, "city": row.city, "region": row.region,
        "country": row.country, "country_raw": row.country_raw, "location_raw": row.location_raw,
        "event_status": row.event_status, "confirmation_status": confirmation,
        "hiatus": int(row.hiatus), "website_url": row.website_url,
    }


def _hash(fields: dict, raw: dict) -> str:
    return hashlib.sha256(json.dumps([fields, raw], sort_keys=True, default=str).encode()).hexdigest()[:16]


def _observe(conn, event_id, run_id, ts, content_hash, raw, previous):
    conn.execute(
        "INSERT INTO event_observations (event_id, sync_run_id, source, observed_at, content_hash, "
        "raw_payload, previous_values) VALUES (?,?,?,?,?,?,?)",
        (event_id, run_id, SOURCE, ts, content_hash, json.dumps(raw, sort_keys=True),
         json.dumps(previous, sort_keys=True) if previous is not None else None))


def _apply_row(conn, row: ParsedEvent, confirmation: str, *, run_id, ts, source_url,
               claimed, seen_keys, stats, warnings, changes):
    warnings.extend(f"{row.name}: {w}" for w in row.warnings)
    series_id = resolve_or_create_series(conn, row.canonical_name, row.aliases, SOURCE, ts, warnings)

    dup_key = (series_id, row.start_date, row.end_date, row.country)
    if dup_key in seen_keys:
        stats["skipped"] += 1
        warnings.append(f"duplicate listing skipped: {row.name} {row.start_date} {row.country}")
        return
    seen_keys.add(dup_key)

    existing = match_occurrence(conn, series_id, row.start_date, row.country, claimed, warnings)
    if existing is not None and confirmation == "unknown":
        confirmation = existing["confirmation_status"]  # don't flap when the 2nd list is unavailable
    fields = _fields_for(row, confirmation)
    raw_json = json.dumps(row.raw, sort_keys=True)
    content_hash = _hash(fields, row.raw)

    if existing is None:
        eid = new_id("evt")
        cols = ["id", "series_id", *fields, "active", "lifecycle_status", "source", "wsdc_source_url",
                "raw_payload", "content_hash", "first_seen_at", "last_seen_at", "last_synced_at",
                "created_at", "updated_at"]
        vals = [eid, series_id, *fields.values(), 1, "listed", SOURCE, source_url, raw_json,
                content_hash, ts, ts, ts, ts, ts]
        conn.execute(f"INSERT INTO events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        _observe(conn, eid, run_id, ts, content_hash, row.raw, None)
        claimed.add(eid)
        stats["added"] += 1
        changes.append({"action": "added", "event_id": eid, "name": row.name,
                        "start_date": fields["start_date"]})
        return

    eid = existing["id"]
    claimed.add(eid)
    diff = {k: {"from": existing[k], "to": v} for k, v in fields.items() if existing[k] != v}
    reactivated = not existing["active"]
    if diff or reactivated:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE events SET {sets}, active=1, lifecycle_status='listed', wsdc_source_url=?, "
            f"raw_payload=?, content_hash=?, last_seen_at=?, last_synced_at=?, updated_at=? WHERE id=?",
            [*fields.values(), source_url, raw_json, content_hash, ts, ts, ts, eid])
        previous = {k: d["from"] for k, d in diff.items()}
        if reactivated:
            previous.update(active=existing["active"], lifecycle_status=existing["lifecycle_status"])
        _observe(conn, eid, run_id, ts, content_hash, row.raw, previous)
        stats["updated"] += 1
        changes.append({"action": "reactivated" if reactivated and not diff else "updated",
                        "event_id": eid, "name": row.name, "changed": diff})
    else:
        conn.execute("UPDATE events SET last_seen_at=?, last_synced_at=? WHERE id=?", (ts, ts, eid))
        stats["unchanged"] += 1


def _deactivate_unseen(conn, claimed, today: date, ts, stats, changes):
    rows = conn.execute(
        "SELECT id, name, end_date, active, lifecycle_status FROM events WHERE source=?", (SOURCE,)
    ).fetchall()
    for r in rows:
        if r["id"] in claimed:
            continue
        lifecycle = "past" if r["end_date"] and r["end_date"] < today.isoformat() else "missing_from_source"
        if r["active"] or r["lifecycle_status"] != lifecycle:
            conn.execute("UPDATE events SET active=0, lifecycle_status=?, last_synced_at=?, updated_at=? "
                         "WHERE id=?", (lifecycle, ts, ts, r["id"]))
            if r["active"]:
                stats["deactivated"] += 1
                changes.append({"action": "deactivated", "event_id": r["id"], "name": r["name"],
                                "lifecycle_status": lifecycle})
        else:
            conn.execute("UPDATE events SET last_synced_at=? WHERE id=?", (ts, r["id"]))


# ------------------------------------------------------------------ entry point
def run_wsdc_sync(conn: sqlite3.Connection, fetch: Callable[[], FetchResult], *,
                  now: datetime | None = None, force: bool = False, guard_ratio: float = GUARD_RATIO,
                  snapshot_dir: str | Path | None = None, source_url: str | None = None) -> dict:
    """Run one synchronization. Returns the sync_runs row as a dict.

    Raises SyncAlreadyRunning if another run holds the lock; every other failure is
    recorded on the run (status 'failed' or 'aborted_guard') rather than raised.
    """
    now = now or utcnow()
    ts, today = iso(now), now.date()
    run_id = _start_run(conn, source_url or "", now)
    stats: Counter = Counter(found=0, added=0, updated=0, unchanged=0, deactivated=0, skipped=0)
    errors: list[str] = []
    warnings: list[str] = []
    changes: list[dict] = []
    snapshot_path = None
    try:
        fetched = fetch()
        source_url = fetched.source_url or source_url or ""
        conn.execute("UPDATE sync_runs SET source_url=? WHERE id=?", (source_url, run_id))
        errors.extend(fetched.errors)
        snapshot_path = _save_snapshot(snapshot_dir, run_id, fetched)

        parsed = parse_event_list(fetched.main_html)
        errors.extend(parsed.errors)
        if not parsed.table_found:
            raise GuardTripped("event table not found in WSDC page; layout may have changed")
        stats["found"] = len(parsed.rows)

        confirmed_keys = None
        if fetched.confirmed_html is not None:
            cp = parse_event_list(fetched.confirmed_html)
            if cp.table_found and (cp.rows or not parsed.rows):
                confirmed_keys = {r.match_key for r in cp.rows}
            else:
                warnings.append("hide-unconfirmed list unusable; confirmation_status left unknown/unchanged")

        _check_guard(conn, stats["found"], force, guard_ratio)

        claimed: set[str] = set()
        seen_keys: set = set()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for row in parsed.rows:
                if confirmed_keys is not None:
                    confirmation = "confirmed" if row.match_key in confirmed_keys else "unconfirmed"
                elif row.unconfirmed_hint:
                    confirmation = "unconfirmed"
                else:
                    confirmation = "unknown"
                _apply_row(conn, row, confirmation, run_id=run_id, ts=ts, source_url=source_url,
                           claimed=claimed, seen_keys=seen_keys, stats=stats, warnings=warnings,
                           changes=changes)
            _deactivate_unseen(conn, claimed, today, ts, stats, changes)
            _finish_run(conn, run_id, "success", stats, errors, warnings, changes, snapshot_path, utcnow())
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    except GuardTripped as e:
        _finish_run(conn, run_id, "aborted_guard", stats, errors + [str(e)], warnings, [], snapshot_path, utcnow())
    except Exception as e:  # noqa: BLE001 - recorded for diagnosis
        _finish_run(conn, run_id, "failed", stats, errors + [f"{type(e).__name__}: {e}"], warnings, [],
                    snapshot_path, utcnow())
    return run_summary(conn, run_id)
