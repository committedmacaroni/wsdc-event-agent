"""Read-side queries and serialization."""
from __future__ import annotations

import json
import sqlite3

from .normalize import normalize_country_filter, normalize_text

MAX_LIMIT = 500
DEFAULT_LIMIT = 50
EVENT_STATUSES = {"registry", "trial", "unknown"}
CONFIRMATION_STATUSES = {"confirmed", "unconfirmed", "unknown"}
LIFECYCLE_STATUSES = {"listed", "past", "missing_from_source", "historical"}


class QueryError(ValueError):
    pass


def _bool(v: str) -> bool:
    s = str(v).strip().lower()
    if s in ("1", "true", "yes"):
        return True
    if s in ("0", "false", "no"):
        return False
    raise QueryError(f"expected true/false, got {v!r}")


def aliases_for(conn, series_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT alias, normalized_alias, source, created_at FROM event_aliases WHERE series_id=? "
        "ORDER BY created_at, alias", (series_id,))]


def serialize_event(conn, r: sqlite3.Row, *, detail: bool = False) -> dict:
    series = conn.execute("SELECT canonical_name FROM event_series WHERE id=?", (r["series_id"],)).fetchone()
    out = {
        "id": r["id"], "series_id": r["series_id"],
        "canonical_name": series["canonical_name"] if series else None,
        "name": r["name"], "normalized_name": r["normalized_name"],
        "start_date": r["start_date"], "end_date": r["end_date"], "year": r["year"],
        "date_precision": r["date_precision"],
        "city": r["city"], "region": r["region"], "country": r["country"],
        "location_raw": r["location_raw"],
        "event_status": r["event_status"], "confirmation_status": r["confirmation_status"],
        "hiatus": bool(r["hiatus"]), "active": bool(r["active"]),
        "lifecycle_status": r["lifecycle_status"], "source": r["source"],
        "website_url": r["website_url"], "wsdc_source_url": r["wsdc_source_url"],
        "aliases": [a["alias"] for a in aliases_for(conn, r["series_id"])],
        "results_available": conn.execute(
            "SELECT 1 FROM provider_events WHERE event_id=? LIMIT 1", (r["id"],)).fetchone() is not None,
        "first_seen_at": r["first_seen_at"], "last_seen_at": r["last_seen_at"],
        "last_synced_at": r["last_synced_at"], "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }
    if detail:
        out["country_raw"] = r["country_raw"]
        out["raw_payload"] = json.loads(r["raw_payload"]) if r["raw_payload"] else None
        out["alias_details"] = aliases_for(conn, r["series_id"])
        out["external_refs"] = [dict(x) for x in conn.execute(
            "SELECT source, external_id, url, created_at FROM event_external_refs WHERE event_id=?",
            (r["id"],))]
    return out


def list_events(conn, params: dict) -> dict:
    where, args = [], []
    if (v := params.get("year")) not in (None, ""):
        try:
            args.append(int(v))
        except ValueError:
            raise QueryError("year must be an integer") from None
        where.append("e.year = ?")
    if v := params.get("country"):
        where.append("e.country = ?"); args.append(normalize_country_filter(v))
    for key, allowed in (("event_status", EVENT_STATUSES), ("confirmation_status", CONFIRMATION_STATUSES),
                         ("lifecycle_status", LIFECYCLE_STATUSES)):
        if v := params.get(key):
            if v not in allowed:
                raise QueryError(f"{key} must be one of {sorted(allowed)}")
            where.append(f"e.{key} = ?"); args.append(v)
    if (v := params.get("active")) not in (None, ""):
        where.append("e.active = ?"); args.append(int(_bool(v)))
    if (v := params.get("results_available")) not in (None, ""):
        where.append(("" if _bool(v) else "NOT ") +
                     "EXISTS (SELECT 1 FROM provider_events pe WHERE pe.event_id = e.id)")
    if v := params.get("series_id"):
        where.append("e.series_id = ?"); args.append(v)
    if v := params.get("date_from"):
        where.append("e.end_date >= ?"); args.append(v)
    if v := params.get("date_to"):
        where.append("e.start_date <= ?"); args.append(v)
    if v := params.get("search"):
        term = f"%{normalize_text(v)}%"
        where.append("(e.normalized_name LIKE ? OR s.normalized_name LIKE ? OR EXISTS "
                     "(SELECT 1 FROM event_aliases a WHERE a.series_id=e.series_id AND a.normalized_alias LIKE ?))")
        args += [term, term, term]
    try:
        limit = min(max(int(params.get("limit") or DEFAULT_LIMIT), 1), MAX_LIMIT)
        offset = max(int(params.get("offset") or 0), 0)
    except ValueError:
        raise QueryError("limit and offset must be integers") from None
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    base = f"FROM events e JOIN event_series s ON s.id = e.series_id {clause}"
    total = conn.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
    rows = conn.execute(f"SELECT e.* {base} ORDER BY e.start_date, e.name, e.id LIMIT ? OFFSET ?",
                        [*args, limit, offset]).fetchall()
    return {"items": [serialize_event(conn, r) for r in rows], "total": total,
            "limit": limit, "offset": offset}


def get_event(conn, event_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    return serialize_event(conn, r, detail=True) if r else None


def get_series(conn, series_id: str) -> dict | None:
    s = conn.execute("SELECT * FROM event_series WHERE id=?", (series_id,)).fetchone()
    if not s:
        return None
    events = conn.execute("SELECT * FROM events WHERE series_id=? ORDER BY start_date", (series_id,)).fetchall()
    return {**dict(s), "aliases": aliases_for(conn, series_id),
            "events": [serialize_event(conn, e) for e in events]}


def list_sync_runs(conn, limit: int = 20) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (min(limit, 200),)):
        d = dict(r)
        for k in ("errors", "warnings", "changes"):
            d[k] = json.loads(d[k] or "[]")
        out.append(d)
    return out
