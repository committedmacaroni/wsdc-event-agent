"""Score sheets, retrieved on demand.

Flow:
  1. discover_eepro_year(): read a provider's event list (one page per year) so past events
     exist in the catalog with a known results source. No score sheets are fetched.
  2. The user enters their name and selects the events they attended.
  3. lookup_for_events(): fetch only those events' sheets (cached), return the dancer's
     entries for every round.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import date, datetime, timedelta

from . import eepro
from .db import iso, utcnow
from .enrichment import import_external_event
from .fetcher import fetch_html
from .normalize import normalize_event_name, normalize_text

ROUND_ORDER = {"prelims": 0, "quarters": 1, "semis": 2, "finals": 3}
REFETCH_RECENT_DAYS = 14        # sheets of events that ended recently may still be corrected
RECENT_SHEET_MAX_AGE = timedelta(hours=1)
INDEX_MAX_AGE = timedelta(hours=24)
REQUEST_DELAY = 0.3             # seconds between provider requests during a lookup
MAX_EVENTS_PER_LOOKUP = 10


# ------------------------------------------------------------------ discovery (event lists only)
def _upsert_provider_event(conn, provider, ev, event_id, index_url, ts):
    conn.execute(
        "INSERT INTO provider_events (provider, provider_key, event_id, name, start_date, end_date, index_url, "
        "links, fetched_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(provider, provider_key) DO UPDATE SET "
        "event_id=COALESCE(excluded.event_id, provider_events.event_id), name=excluded.name, "
        "start_date=excluded.start_date, end_date=excluded.end_date, index_url=excluded.index_url, "
        "links=excluded.links, fetched_at=excluded.fetched_at",
        (provider, ev.key, event_id, ev.name, ev.start.isoformat() if ev.start else None,
         ev.end.isoformat() if ev.end else None, index_url, json.dumps(ev.links), ts))


def discover_eepro_year(conn, year: int, *, fetch=fetch_html, now: datetime | None = None) -> dict:
    """Add eepro's events for a year to the catalog and remember their result-page links."""
    now = now or utcnow()
    ts = iso(now)
    index_url = eepro.INDEX_URL.format(year=year)
    run_id = conn.execute("INSERT INTO sync_runs (source, source_url, started_at, status) "
                          "VALUES (?,?,?, 'running')", (f"discover:{eepro.PROVIDER}", index_url, ts)).lastrowid
    stats = {"events": 0, "matched": 0, "created": 0}
    errors, changes, status = [], [], "success"
    try:
        events, errors = eepro.parse_index(fetch(index_url), index_url)
        if not events:
            raise RuntimeError("no events found on the eepro index page; layout may have changed")
        for ev in events:
            event_id = None
            if ev.start:
                try:
                    linked = import_external_event(conn, {
                        "source": eepro.PROVIDER, "name": ev.name, "start_date": ev.start.isoformat(),
                        "end_date": ev.end.isoformat(), "external_id": ev.key, "url": index_url}, now=now)
                    event_id = linked["event_id"]
                    stats[linked["status"]] += 1
                    changes.append({"event": ev.name, "key": ev.key, "catalog_event_id": event_id,
                                    "catalog_link": linked["status"]})
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{ev.key}: catalog link failed: {e}")
            _upsert_provider_event(conn, eepro.PROVIDER, ev, event_id, index_url, ts)
            stats["events"] += 1
    except Exception as e:  # noqa: BLE001
        status = "failed"
        errors.append(f"{type(e).__name__}: {e}")
    conn.execute("UPDATE sync_runs SET status=?, completed_at=?, records_found=?, records_added=?, "
                 "records_updated=?, errors=?, changes=? WHERE id=?",
                 (status, iso(utcnow()), stats["events"], stats["created"], stats["matched"],
                  json.dumps(errors[:200]), json.dumps(changes[:500]), run_id))
    return {"run_id": run_id, "status": status, "provider": eepro.PROVIDER, "year": year, **stats,
            "errors": errors[:50]}


def _index_fresh(conn, year: int, now: datetime) -> bool:
    r = conn.execute("SELECT completed_at FROM sync_runs WHERE source=? AND source_url=? AND status='success' "
                     "ORDER BY id DESC LIMIT 1",
                     (f"discover:{eepro.PROVIDER}", eepro.INDEX_URL.format(year=year))).fetchone()
    if not r or not r["completed_at"]:
        return False
    return now - datetime.fromisoformat(r["completed_at"].replace("Z", "+00:00")) < INDEX_MAX_AGE


# ------------------------------------------------------------------ sheet storage
def store_sheet(conn, url: str, provider: str, event_key: str | None, event_id: str | None,
                parsed: dict, html: str, ts: str) -> int:
    rows = []
    for sec in parsed["sections"]:
        for e in sec["entries"]:
            rows.append((url, event_id, provider, sec["title"], sec["division"], sec["round"], e["role"],
                         e["name"], normalize_text(e["name"]), e["partner"], e["bib"], e.get("place"),
                         e.get("rank"), json.dumps(e["marks"]), e["counts"], e["score"],
                         None if e["advanced"] is None else int(e["advanced"]), e["alternate"], sec["competed"]))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM scoresheet_entries WHERE sheet_url=?", (url,))
        conn.execute(
            "INSERT INTO scoresheet_sheets (url, provider, provider_event_key, event_id, title, status, entries, "
            "content_hash, error, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
            "provider_event_key=excluded.provider_event_key, event_id=excluded.event_id, title=excluded.title, "
            "status=excluded.status, entries=excluded.entries, content_hash=excluded.content_hash, "
            "error=excluded.error, fetched_at=excluded.fetched_at",
            (url, provider, event_key, event_id, parsed.get("event_title"), "parsed" if rows else "empty",
             len(rows), hashlib.sha256(html.encode()).hexdigest()[:16],
             "; ".join(parsed["errors"][:5]) or None, ts))
        conn.executemany(
            "INSERT INTO scoresheet_entries (sheet_url, event_id, provider, section, division, round, role, "
            "competitor_name, normalized_name, partner_name, bib, place, rank, marks, counts, score, advanced, "
            "alternate, competed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


def _sheet_is_fresh(conn, url: str, event_end: str | None, now: datetime) -> bool:
    r = conn.execute("SELECT status, fetched_at FROM scoresheet_sheets WHERE url=?", (url,)).fetchone()
    if not r or r["status"] not in ("parsed", "empty"):
        return False
    ended_long_ago = event_end and (now.date() - date.fromisoformat(event_end)).days > REFETCH_RECENT_DAYS
    fetched = datetime.fromisoformat(r["fetched_at"].replace("Z", "+00:00"))
    return bool(ended_long_ago) or now - fetched < RECENT_SHEET_MAX_AGE


# ------------------------------------------------------------------ matching a catalog event to provider pages
def _provider_events_for(conn, ev) -> list:
    rows = conn.execute("SELECT * FROM provider_events WHERE event_id=?", (ev["id"],)).fetchall()
    if rows or not ev["start_date"]:
        return list(rows)
    # Fallback: same dates (±3 days) and a similar name, for events the provider names differently.
    start = date.fromisoformat(ev["start_date"])
    names = {normalize_event_name(ev["name"])}
    names |= {r["normalized_alias"] for r in conn.execute(
        "SELECT normalized_alias FROM event_aliases WHERE series_id=?", (ev["series_id"],))}
    names |= {r["normalized_name"] for r in conn.execute(
        "SELECT normalized_name FROM event_series WHERE id=?", (ev["series_id"],))}
    out = []
    for r in conn.execute("SELECT * FROM provider_events WHERE start_date BETWEEN ? AND ?",
                          ((start - timedelta(days=3)).isoformat(), (start + timedelta(days=3)).isoformat())):
        pn = set(normalize_event_name(r["name"] or "").split())
        if any(pn and n and len(pn & set(n.split())) / len(pn | set(n.split())) >= 0.5 for n in names):
            out.append(r)
    return out


# ------------------------------------------------------------------ result shaping
def _group_divisions(rows) -> list[dict]:
    divs: dict = {}
    for r in rows:
        d = divs.setdefault((r["division"], r["role"]), {"division": r["division"], "role": r["role"], "rounds": []})
        d["rounds"].append({
            "round": r["round"], "section": r["section"], "bib": r["bib"], "partner": r["partner_name"],
            "rank": r["rank"], "place": r["place"], "marks": json.loads(r["marks"]) if r["marks"] else None,
            "counts": r["counts"], "score": r["score"],
            "advanced": None if r["advanced"] is None else bool(r["advanced"]),
            "alternate": r["alternate"], "competed": r["competed"], "sheet_url": r["sheet_url"]})
    out = []
    for d in divs.values():
        d["rounds"].sort(key=lambda x: ROUND_ORDER.get(x["round"], 9))
        finals = [x for x in d["rounds"] if x["round"] == "finals"]
        d["furthest_round"] = d["rounds"][-1]["round"] if d["rounds"] else None
        d["final_place"] = finals[0]["place"] if finals else None
        out.append(d)
    return out


# ------------------------------------------------------------------ on-demand lookup
def lookup_for_events(conn, name: str, event_ids: list[str], *, fetch=fetch_html,
                      now: datetime | None = None, delay: float = REQUEST_DELAY) -> dict:
    """Fetch (or reuse cached) score sheets for the selected events and return this dancer's rounds.

    Per-event status:
      found            - the dancer appears on at least one sheet
      not_on_sheets    - sheets were checked and the name wasn't on them
      no_results_source- no supported provider has results for this event (yet)
      event_not_found  - unknown event_id
    """
    norm = normalize_text(name)
    if len(norm) < 3:
        raise ValueError("name must be at least 3 characters")
    ids = list(dict.fromkeys(i for i in (event_ids or []) if i))
    if not ids:
        raise ValueError("select at least one event (event_ids)")
    if len(ids) > MAX_EVENTS_PER_LOOKUP:
        raise ValueError(f"at most {MAX_EVENTS_PER_LOOKUP} events per lookup")
    now = now or utcnow()
    ts = iso(now)
    discovered_years: set[int] = set()
    results = []
    for eid in ids:
        ev = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not ev:
            results.append({"event_id": eid, "status": "event_not_found"})
            continue
        pes = _provider_events_for(conn, ev)
        if not pes and ev["year"] and ev["year"] not in discovered_years and not _index_fresh(conn, ev["year"], now):
            discovered_years.add(ev["year"])
            discover_eepro_year(conn, ev["year"], fetch=fetch, now=now)
            ev = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
            pes = _provider_events_for(conn, ev)
        base = {"event_id": eid, "event_name": ev["name"], "start_date": ev["start_date"],
                "end_date": ev["end_date"]}
        if not pes:
            results.append({**base, "status": "no_results_source", "divisions": [], "sheets": [],
                            "message": "No supported scoring provider has results for this event yet."})
            continue
        sheets, sheet_errors = [], []
        for pe in pes:
            for title, url in json.loads(pe["links"]):
                if not url.lower().endswith((".html", ".htm")):
                    sheets.append({"title": title, "url": url, "status": "skipped_pdf"})
                    continue
                if _sheet_is_fresh(conn, url, pe["end_date"] or ev["end_date"], now):
                    conn.execute("UPDATE scoresheet_entries SET event_id=? WHERE sheet_url=?", (eid, url))
                    sheets.append({"title": title, "url": url, "status": "cached"})
                    continue
                try:
                    html = fetch(url)
                    parsed = eepro.parse_sheet(html)
                    store_sheet(conn, url, pe["provider"], pe["provider_key"], eid, parsed, html, ts)
                    sheets.append({"title": title, "url": url, "status": "fetched"})
                except Exception as e:  # noqa: BLE001
                    sheet_errors.append(f"{url}: {e}")
                    sheets.append({"title": title, "url": url, "status": "error"})
                if delay:
                    time.sleep(delay)
        rows = conn.execute("SELECT * FROM scoresheet_entries WHERE event_id=? AND normalized_name=?",
                            (eid, norm)).fetchall()
        results.append({**base, "status": "found" if rows else "not_on_sheets",
                        "provider": pes[0]["provider"], "divisions": _group_divisions(rows),
                        "sheets": sheets, "errors": sheet_errors})
    return {"name": name, "normalized_name": norm, "events": results,
            "note": "Matched on the exact name printed on score sheets (case and accents ignored)."}


def search_by_name(conn, name: str, *, year: int | None = None, event_id: str | None = None) -> dict:
    """Search sheets that have already been retrieved (no provider requests)."""
    norm = normalize_text(name)
    if len(norm) < 3:
        raise ValueError("name must be at least 3 characters")
    sql = ("SELECT se.*, e.name AS event_name, e.start_date, e.end_date FROM scoresheet_entries se "
           "LEFT JOIN events e ON e.id = se.event_id WHERE se.normalized_name = ?")
    args: list = [norm]
    if year:
        sql += " AND e.year = ?"; args.append(int(year))
    if event_id:
        sql += " AND se.event_id = ?"; args.append(event_id)
    by_event: dict = {}
    for r in conn.execute(sql, args):
        by_event.setdefault(r["event_id"], {"event_id": r["event_id"], "event_name": r["event_name"],
                                            "start_date": r["start_date"], "rows": []})["rows"].append(r)
    events = []
    for ev in sorted(by_event.values(), key=lambda e: e["start_date"] or "", reverse=True):
        events.append({k: ev[k] for k in ("event_id", "event_name", "start_date")} |
                      {"divisions": _group_divisions(ev.pop("rows"))})
    return {"name": name, "normalized_name": norm, "events": events}


def coverage(conn) -> dict:
    return {
        "provider_events": [dict(r) for r in conn.execute(
            "SELECT provider, COUNT(*) n, MIN(start_date) first, MAX(start_date) last FROM provider_events "
            "GROUP BY provider")],
        "sheets_cached": [dict(r) for r in conn.execute(
            "SELECT provider, status, COUNT(*) n FROM scoresheet_sheets GROUP BY provider, status")],
    }
