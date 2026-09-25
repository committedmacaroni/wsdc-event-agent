"""Historical enrichment: add events found on scoring providers / competitor records."""
from __future__ import annotations

import json
from datetime import date

from .db import iso, new_id, utcnow
from .normalize import country_code_from_name, normalize_event_name, split_event_name
from .sync import add_alias, match_occurrence, resolve_or_create_series

IMPORT_MATCH_DAYS = 7
RESERVED_SOURCES = {"wsdc_calendar"}


class ImportError_(ValueError):
    pass


def import_external_event(conn, payload: dict, now=None) -> dict:
    """payload: {source, name, start_date, end_date?, city?, region?, country?, external_id?, url?}"""
    source = (payload.get("source") or "").strip()
    name = (payload.get("name") or "").strip()
    if not source or source in RESERVED_SOURCES:
        raise ImportError_("source is required and may not be 'wsdc_calendar'")
    if not name or not payload.get("start_date"):
        raise ImportError_("name and start_date are required")
    try:
        start = date.fromisoformat(payload["start_date"])
        end = date.fromisoformat(payload.get("end_date") or payload["start_date"])
    except ValueError:
        raise ImportError_("dates must be YYYY-MM-DD") from None
    country = country_code_from_name(payload.get("country")) or (payload.get("country") or "").upper() or None
    ts = iso(now or utcnow())
    warnings: list[str] = []

    conn.execute("BEGIN IMMEDIATE")
    try:
        canonical, aliases = split_event_name(name)
        series_id = resolve_or_create_series(conn, canonical, aliases, source, ts, warnings)
        add_alias(conn, series_id, name, source, ts)  # provider's own name, never the canonical
        existing = match_occurrence(conn, series_id, start, country, set(), warnings,
                                    source=None, window_days=IMPORT_MATCH_DAYS)
        if existing is None:  # a month-precision record (e.g. from the WSDC registry) in the same month
            existing = conn.execute(
                "SELECT * FROM events WHERE series_id=? AND date_precision='month' AND substr(start_date,1,7)=? "
                "ORDER BY created_at LIMIT 1", (series_id, start.isoformat()[:7])).fetchone()
        if existing:
            event_id, status = existing["id"], "matched"
            if existing["date_precision"] == "month":
                conn.execute("UPDATE events SET start_date=?, end_date=?, year=?, date_precision='day', updated_at=? "
                             "WHERE id=?", (start.isoformat(), end.isoformat(), start.year, ts, event_id))
        else:
            event_id, status = new_id("evt"), "created"
            conn.execute(
                "INSERT INTO events (id, series_id, name, normalized_name, start_date, end_date, year, "
                "city, region, country, location_raw, event_status, confirmation_status, hiatus, active, "
                "lifecycle_status, source, website_url, raw_payload, first_seen_at, last_seen_at, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'unknown','unknown',0,0, "
                "'historical',?,?,?,?,?,?,?)",
                (event_id, series_id, name, normalize_event_name(name), start.isoformat(), end.isoformat(),
                 start.year, payload.get("city"), payload.get("region"), country,
                 payload.get("location_raw"), source, payload.get("url"),
                 json.dumps(payload, sort_keys=True), ts, ts, ts, ts))
        if payload.get("external_id") or payload.get("url"):
            conn.execute(
                "INSERT OR IGNORE INTO event_external_refs (event_id, source, external_id, url, created_at) "
                "VALUES (?,?,?,?,?)",
                (event_id, source, payload.get("external_id") or payload.get("url"), payload.get("url"), ts))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"status": status, "event_id": event_id, "series_id": series_id, "warnings": warnings}
