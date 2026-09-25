"""WSDC points registry: competitor lookup by WSDC ID or name, linked to the event catalog.

Source: POST https://points.worldsdc.com/lookup2020/find with form field q=<wsdc id or name>.
The response format was confirmed against a live record on 2026-09-24:
  {"leader": {...}, "follower": {...}, "dancer_first": ..., "dancer_wsdcid": ...}
  each role block: {"dancer": {...}, "placements": [] | {style: {div_abbr: {division, total_points,
                    competitions: [{role, points, result, event: {id, name, location, url, date}}]}}}}
  event "date" is month precision, e.g. "September 2026".

Limitation: the registry only records results that earned points. Prelims, semis and
finals without points are not here; those need scoring-provider score sheets.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .db import iso, new_id, utcnow
from .normalize import country_code_from_name, normalize_event_name, normalize_text, parse_location, split_event_name
from .sync import add_alias, create_series, find_series

FIND_URL = "https://points.worldsdc.com/lookup2020/find"
USER_AGENT = "WSDCEventCatalog/0.2 (WCS Results Agent)"
CACHE_TTL = timedelta(hours=12)
SOURCE = "wsdc_registry"
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], start=1)}


class RegistryError(Exception):
    pass


# ------------------------------------------------------------------ fetching
def post_find(q: str, timeout: float = 20) -> object:
    data = urllib.parse.urlencode({"q": q}).encode()
    req = urllib.request.Request(FIND_URL, data=data, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        raise RegistryError(f"WSDC registry request failed: {e}") from None
    try:
        return json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        raise RegistryError(f"WSDC registry returned non-JSON: {body[:200]!r}") from None


def cached_find(conn, q: str, fetch=post_find, now: datetime | None = None, max_age=CACHE_TTL):
    """Registry lookup with a small cache so repeated lookups don't hammer WSDC."""
    now = now or utcnow()
    key = normalize_text(q) or q.strip()
    row = conn.execute("SELECT payload, fetched_at FROM registry_cache WHERE query=?", (key,)).fetchone()
    if row:
        fetched = datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
        if now - fetched < max_age:
            return json.loads(row["payload"]), row["fetched_at"]
    payload = fetch(q)
    conn.execute("INSERT INTO registry_cache (query, payload, fetched_at) VALUES (?,?,?) "
                 "ON CONFLICT(query) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
                 (key, json.dumps(payload), iso(now)))
    return payload, iso(now)


# ------------------------------------------------------------------ parsing
def parse_month(s: str | None) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*([A-Za-z]+)\.?\s+(\d{4})\s*", s or "")
    if not m:
        return None
    word = m.group(1).lower()
    month = _MONTHS.get(word) or next((v for k, v in _MONTHS.items() if k.startswith(word[:3])), None)
    return (int(m.group(2)), month) if month else None


def _candidate(d: dict) -> dict | None:
    wid = d.get("wscid") or d.get("wsdcid") or d.get("dancer_wsdcid")
    if wid is None:
        return None
    return {"wsdc_id": int(wid), "first_name": d.get("first_name"), "last_name": d.get("last_name")}


def parse_find(payload) -> dict:
    """Normalize a registry response into {"type": "dancer" | "candidates" | "none", ...}."""
    if payload in (None, "", [], {}):
        return {"type": "none"}
    if isinstance(payload, list):
        cands = [c for c in (_candidate(x) for x in payload if isinstance(x, dict)) if c]
        return {"type": "candidates", "candidates": cands} if cands else {"type": "none"}
    if not isinstance(payload, dict):
        raise RegistryError(f"unrecognized registry response type {type(payload).__name__}")
    if isinstance(payload.get("names"), list):
        cands = [c for c in (_candidate(x) for x in payload["names"] if isinstance(x, dict)) if c]
        return {"type": "candidates", "candidates": cands} if cands else {"type": "none"}
    if not any(k in payload for k in ("leader", "follower", "dominate_data", "dancer_wsdcid")):
        if payload.get("type") in ("none", "error") or payload.get("error"):
            return {"type": "none"}
        raise RegistryError(f"unrecognized registry response keys: {sorted(payload)[:10]}")

    dancer = {}
    for block in (payload.get("dominate_data"), payload.get("follower"), payload.get("leader")):
        if isinstance(block, dict) and isinstance(block.get("dancer"), dict):
            dancer = block["dancer"]
            break
    placements = []
    for role_key in ("leader", "follower"):
        block = payload.get(role_key)
        pl = block.get("placements") if isinstance(block, dict) else None
        if not isinstance(pl, dict):
            continue  # [] means no results in this role
        for style, divisions in pl.items():
            if not isinstance(divisions, dict):
                continue
            for abbr, div in divisions.items():
                if not isinstance(div, dict):
                    continue
                division = div.get("division") or {}
                for c in div.get("competitions") or []:
                    ev = c.get("event") or {}
                    ym = parse_month(ev.get("date"))
                    placements.append({
                        "role": c.get("role") or role_key,
                        "style": style,
                        "division": division.get("name") or abbr,
                        "division_abbr": division.get("abbreviation") or abbr,
                        "result": str(c.get("result")) if c.get("result") is not None else None,
                        "points": c.get("points"),
                        "registry_event": {
                            "id": ev.get("id"), "name": ev.get("name"), "location": ev.get("location"),
                            "url": ev.get("url"), "date": ev.get("date"),
                            "month": f"{ym[0]}-{ym[1]:02d}" if ym else None,
                        },
                    })
    placements.sort(key=lambda p: (p["registry_event"]["month"] or ""), reverse=True)

    def role_summary(block_key, level_key, points_key):
        return {"role": payload.get(block_key), "highest_level": payload.get(level_key),
                "highest_level_points": payload.get(points_key)}

    return {
        "type": "dancer",
        "wsdc_id": int(payload.get("dancer_wsdcid") or dancer.get("wscid")),
        "first_name": payload.get("dancer_first") or dancer.get("first_name"),
        "last_name": payload.get("dancer_last") or dancer.get("last_name"),
        "primary_role": payload.get("short_dominate_role"),
        "secondary_role": payload.get("short_non_dominate_role"),
        "primary": role_summary("short_dominate_role", "dominate_role_highest_level",
                                "dominate_role_highest_level_points"),
        "secondary": role_summary("short_non_dominate_role", "non_dominate_role_highest_level",
                                  "non_dominate_role_highest_level_points"),
        "level_required": payload.get("dominate_required"),
        "level_allowed": payload.get("dominate_allowed"),
        "is_pro": bool(payload.get("is_pro")),
        "placements": placements,
    }


# ------------------------------------------------------------------ catalog linking
def _series_for_registry_event(conn, ev: dict, ts: str, warnings: list) -> str | None:
    name = (ev.get("name") or "").strip()
    ext_id = str(ev["id"]) if ev.get("id") is not None else None
    if ext_id:
        r = conn.execute("SELECT series_id FROM series_external_refs WHERE source=? AND external_id=?",
                         (SOURCE, ext_id)).fetchone()
        if r:
            add_alias(conn, r["series_id"], name, SOURCE, ts)
            return r["series_id"]
    if not name:
        return None
    canonical, aliases = split_event_name(name)
    sid = find_series(conn, canonical, aliases, warnings) or create_series(conn, canonical, SOURCE, ts)
    for a in aliases:
        add_alias(conn, sid, a, SOURCE, ts)
    if ext_id:
        conn.execute("INSERT OR IGNORE INTO series_external_refs (series_id, source, external_id, created_at) "
                     "VALUES (?,?,?,?)", (sid, SOURCE, ext_id, ts))
    return sid


def link_registry_event(conn, ev: dict, ts: str, warnings: list) -> tuple[str | None, str]:
    """Find (or create as historical) the catalog occurrence for a registry result.

    Returns (event_id, "matched" | "created" | "unmatched").
    """
    sid = _series_for_registry_event(conn, ev, ts, warnings)
    month = ev.get("month")
    if not sid or not month:
        return None, "unmatched"
    rows = conn.execute(
        "SELECT id FROM events WHERE series_id=? AND (substr(start_date,1,7)=? OR substr(end_date,1,7)=?) "
        "ORDER BY CASE date_precision WHEN 'day' THEN 0 ELSE 1 END, created_at", (sid, month, month)).fetchall()
    if len(rows) > 1:
        warnings.append(f"{ev.get('name')} {month}: {len(rows)} catalog events in that month; linked the first")
    if rows:
        return rows[0]["id"], "matched"
    loc = parse_location(ev.get("location"))
    year, mon = int(month[:4]), int(month[5:7])
    start = f"{year}-{mon:02d}-01"
    eid = new_id("evt")
    conn.execute(
        "INSERT INTO events (id, series_id, name, normalized_name, start_date, end_date, year, date_precision, "
        "city, region, country, location_raw, event_status, confirmation_status, hiatus, active, "
        "lifecycle_status, source, website_url, raw_payload, first_seen_at, last_seen_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?, 'month', ?,?,?,?, 'unknown','unknown',0,0, 'historical', ?,?,?,?,?,?,?)",
        (eid, sid, ev.get("name"), normalize_event_name(ev.get("name") or ""), start, start, year,
         loc["city"], loc["region"], loc["country_code"] or country_code_from_name(ev.get("location")),
         ev.get("location"), SOURCE, ev.get("url"), json.dumps(ev, sort_keys=True), ts, ts, ts, ts))
    return eid, "created"


# ------------------------------------------------------------------ public operations
def search_competitors(conn, q: str, fetch=post_find) -> dict:
    q = (q or "").strip()
    if len(q) < 2:
        raise ValueError("search query must be at least 2 characters")
    payload, fetched_at = cached_find(conn, q, fetch)
    parsed = parse_find(payload)
    if parsed["type"] == "dancer":
        items = [{"wsdc_id": parsed["wsdc_id"], "first_name": parsed["first_name"],
                  "last_name": parsed["last_name"]}]
    elif parsed["type"] == "candidates":
        items = parsed["candidates"]
    else:
        items = []
    return {"query": q, "items": items, "source": SOURCE, "fetched_at": fetched_at}


def lookup_competitor(conn, wsdc_id: int | str, fetch=post_find, now: datetime | None = None) -> dict | None:
    wsdc_id = str(wsdc_id).strip()
    if not wsdc_id.isdigit():
        raise ValueError("wsdc_id must be numeric")
    payload, fetched_at = cached_find(conn, wsdc_id, fetch, now)
    parsed = parse_find(payload)
    if parsed["type"] != "dancer" or str(parsed["wsdc_id"]) != wsdc_id:
        return None
    ts = iso(now or utcnow())
    warnings: list[str] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for p in parsed["placements"]:
            eid, status = link_registry_event(conn, p["registry_event"], ts, warnings)
            p["catalog_event_id"], p["catalog_link"] = eid, status
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    for p in parsed["placements"]:
        if p["catalog_event_id"]:
            r = conn.execute("SELECT name, start_date, end_date, date_precision FROM events WHERE id=?",
                             (p["catalog_event_id"],)).fetchone()
            p["catalog_event"] = dict(r) if r else None
    parsed.pop("type")
    return {**parsed, "source": SOURCE, "fetched_at": fetched_at, "warnings": warnings,
            "coverage_note": "WSDC registry lists only results that earned points; "
                             "prelims and non-pointed finals require score sheets."}
