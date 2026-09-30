"""Recognize and merge the same event recorded under different names by different sources.

Example: the WSDC registry says "Swingtacular: The Galactic Open" (August 2026) while the score
sheets say "Swingtacular" (Aug 6-9 2026). They're one event when:
  - their names are related: after dropping generic words, one name's words are all contained
    in the other's ("swingtacular" is in "swingtacular galactic"), and
  - their dates agree: day-precision starts within 7 days, or the same month when either side
    only has a month (registry results).
Two events from the same source are never merged (the WSDC calendar legitimately lists two
"WCS Festival" events a day apart, for example).

Merging never deletes: the duplicate keeps its id with merged_into set, so ids Replit already
stored keep working (GET /events/<old id> returns the surviving event).
"""
from __future__ import annotations

from datetime import date

from .normalize import normalize_event_name, normalize_text

GENERIC = {"the", "a", "an", "of", "and", "wcs", "west", "coast", "swing", "westie", "dance", "dancing",
           "festival", "fest", "championships", "championship", "open", "classic", "convention", "event",
           "weekend", "jam", "invitational", "international", "national", "presents"}
SOURCE_RANK = {"wsdc_calendar": 0, "eepro": 1, "scoring_dance": 1, "manual": 2, "wsdc_registry": 3}
DAY_WINDOW = 7


def _key_words(name: str) -> set[str]:
    words = set(normalize_event_name(name or "").split())
    core = words - GENERIC
    return core or words


def names_related(a: str, b: str) -> bool:
    wa, wb = _key_words(a), _key_words(b)
    if not wa or not wb:
        return False
    small, big = sorted((wa, wb), key=len)
    return small <= big


def _event_names(conn, ev) -> list[str]:
    names = [ev["name"]]
    s = conn.execute("SELECT canonical_name FROM event_series WHERE id=?", (ev["series_id"],)).fetchone()
    if s:
        names.append(s["canonical_name"])
    names += [r["alias"] for r in conn.execute("SELECT alias FROM event_aliases WHERE series_id=?", (ev["series_id"],))]
    return names


def _dates_compatible(a_start, a_prec, b_start, b_prec, a_end=None, b_end=None) -> bool:
    if not a_start or not b_start:
        return False
    if a_prec == "month" or b_prec == "month":
        months_a = {a_start[:7], (a_end or a_start)[:7]}
        months_b = {b_start[:7], (b_end or b_start)[:7]}
        return bool(months_a & months_b)
    return abs((date.fromisoformat(a_start) - date.fromisoformat(b_start)).days) <= DAY_WINDOW


def find_related_event(conn, name: str, start: str, precision: str = "day", end: str | None = None,
                       source: str | None = None):
    """An existing event (any series) with a related name and compatible dates, or None."""
    lo, hi = start[:4] + "-01-01", start[:4] + "-12-31"
    best = None
    for ev in conn.execute("SELECT * FROM events WHERE merged_into IS NULL AND start_date BETWEEN ? AND ?", (lo, hi)):
        if source and ev["source"] == source:
            continue
        if not _dates_compatible(start, precision, ev["start_date"], ev["date_precision"], end, ev["end_date"]):
            continue
        if not any(names_related(name, n) for n in _event_names(conn, ev)):
            continue
        rank = (0 if ev["date_precision"] == "day" else 1, SOURCE_RANK.get(ev["source"], 2), ev["created_at"])
        if best is None or rank < best[0]:
            best = (rank, ev)
    return best[1] if best else None


def merge_events(conn, keep_id: str, drop_id: str) -> None:
    """Move everything that points at drop_id onto keep_id and mark drop_id as merged."""
    keep = conn.execute("SELECT * FROM events WHERE id=?", (keep_id,)).fetchone()
    drop = conn.execute("SELECT * FROM events WHERE id=?", (drop_id,)).fetchone()
    if not keep or not drop or keep_id == drop_id:
        return
    for table in ("scoresheet_entries", "scoresheet_sheets", "provider_events", "event_external_refs",
                  "event_observations"):
        conn.execute(f"UPDATE {table} SET event_id=? WHERE event_id=?", (keep_id, drop_id))
    if conn.execute("SELECT 1 FROM website_checks WHERE event_id=?", (keep_id,)).fetchone():
        conn.execute("DELETE FROM website_checks WHERE event_id=?", (drop_id,))
    else:
        conn.execute("UPDATE website_checks SET event_id=? WHERE event_id=?", (keep_id, drop_id))
    if drop["series_id"] != keep["series_id"]:
        from .db import iso, utcnow
        from .sync import add_alias
        ts = iso(utcnow())
        for n in _event_names(conn, drop):
            add_alias(conn, keep["series_id"], n, drop["source"], ts)
        others = conn.execute("SELECT 1 FROM events WHERE series_id=? AND id<>? AND merged_into IS NULL",
                              (drop["series_id"], drop_id)).fetchone()
        if not others:
            conn.execute("UPDATE OR IGNORE series_external_refs SET series_id=? WHERE series_id=?",
                         (keep["series_id"], drop["series_id"]))
    if keep["date_precision"] == "month" and drop["date_precision"] == "day":
        conn.execute("UPDATE events SET start_date=?, end_date=?, year=?, date_precision='day' WHERE id=?",
                     (drop["start_date"], drop["end_date"], drop["year"], keep_id))
    for col in ("website_url", "city", "region", "country"):
        if not keep[col] and drop[col]:
            conn.execute(f"UPDATE events SET {col}=? WHERE id=?", (drop[col], keep_id))
    conn.execute("UPDATE events SET merged_into=?, active=0 WHERE id=?", (keep_id, drop_id))
    conn.execute("UPDATE events SET merged_into=? WHERE merged_into=?", (keep_id, drop_id))  # chains


def _rank(ev):
    return (0 if ev["date_precision"] == "day" else 1, SOURCE_RANK.get(ev["source"], 2), ev["created_at"], ev["id"])


def merge_duplicates(conn) -> dict:
    """Scan the catalog for cross-source duplicates and merge them. Safe to run repeatedly."""
    merged = []
    events = conn.execute("SELECT * FROM events WHERE merged_into IS NULL AND start_date IS NOT NULL "
                          "ORDER BY start_date").fetchall()
    gone: set[str] = set()
    names = {ev["id"]: _event_names(conn, ev) for ev in events}
    for i, a in enumerate(events):
        if a["id"] in gone:
            continue
        for b in events[i + 1:]:
            if b["id"] in gone or b["source"] == a["source"]:
                continue
            if b["start_date"][:4] != a["start_date"][:4] and b["start_date"][:7] != a["start_date"][:7]:
                if b["start_date"] > a["start_date"][:4] + "-12-31":
                    break
                continue
            if not _dates_compatible(a["start_date"], a["date_precision"], b["start_date"], b["date_precision"],
                                     a["end_date"], b["end_date"]):
                continue
            if not any(names_related(x, y) for x in names[a["id"]] for y in names[b["id"]]):
                continue
            keep, drop = sorted((a, b), key=_rank)
            conn.execute("BEGIN IMMEDIATE")
            try:
                merge_events(conn, keep["id"], drop["id"])
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            gone.add(drop["id"])
            merged.append({"kept": keep["id"], "kept_name": keep["name"], "merged": drop["id"],
                           "merged_name": drop["name"], "sources": [keep["source"], drop["source"]]})
            if drop["id"] == a["id"]:
                break
    return {"merged": len(merged), "pairs": merged}
