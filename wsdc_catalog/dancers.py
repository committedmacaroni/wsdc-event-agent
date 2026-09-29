"""One call for Replit: a dancer's name in, every event they appear in out.

Combines two sources:
  - the score-sheet index (every round, every indexed event), searched by exact name
  - the WSDC points registry (point-earning results at any event, including events
    whose score sheets aren't indexed), when the name matches exactly one registry dancer
"""
from __future__ import annotations

import json

from .normalize import normalize_text
from .registry import RegistryError, lookup_competitor, post_find, search_competitors
from .scoresheets import _group_divisions, index_status


def _event_info(conn, event_id: str | None) -> dict:
    if not event_id:
        return {"event_id": None}
    r = conn.execute("SELECT id, name, start_date, end_date, date_precision, city, region, country "
                     "FROM events WHERE id=?", (event_id,)).fetchone()
    if not r:
        return {"event_id": event_id}
    return {"event_id": r["id"], "event_name": r["name"], "start_date": r["start_date"],
            "end_date": r["end_date"], "date_precision": r["date_precision"], "city": r["city"],
            "region": r["region"], "country": r["country"]}


def _registry_for(conn, name: str, wsdc_id, fetch) -> dict:
    """Registry record for this dancer, or why there isn't one. Never raises."""
    try:
        if wsdc_id:
            rec = lookup_competitor(conn, wsdc_id, fetch=fetch)
            return {"status": "found", "record": rec} if rec else {"status": "not_found"}
        norm = normalize_text(name)
        cands = [c for c in search_competitors(conn, name, fetch=fetch)["items"]
                 if normalize_text(f"{c.get('first_name') or ''} {c.get('last_name') or ''}") == norm]
        if not cands:
            return {"status": "not_found"}
        if len(cands) > 1:
            return {"status": "multiple", "candidates": cands,
                    "message": "Several WSDC dancers share this name; send wsdc_id to include registry results."}
        rec = lookup_competitor(conn, cands[0]["wsdc_id"], fetch=fetch)
        return {"status": "found", "record": rec} if rec else {"status": "not_found"}
    except (RegistryError, ValueError) as e:
        return {"status": "unavailable", "message": str(e)}


def dancer_results(conn, name: str, *, wsdc_id=None, year: int | None = None,
                   include_registry: bool = True, registry_fetch=post_find) -> dict:
    norm = normalize_text(name)
    if len(norm) < 3:
        raise ValueError("name must be at least 3 characters")

    registry = {"status": "skipped"}
    if include_registry:
        registry = _registry_for(conn, name, wsdc_id, registry_fetch)
    known_id = registry["record"]["wsdc_id"] if registry.get("status") == "found" else (
        int(wsdc_id) if wsdc_id and str(wsdc_id).isdigit() else None)

    rows = conn.execute(
        "SELECT se.*, s.provider_event_key FROM scoresheet_entries se "
        "JOIN scoresheet_sheets s ON s.url = se.sheet_url "
        "WHERE (se.normalized_name = ? AND (se.wsdc_id IS NULL OR ? IS NULL OR se.wsdc_id = ?)) "
        "OR (? IS NOT NULL AND se.wsdc_id = ?)",
        (norm, known_id, known_id, known_id, known_id)).fetchall()
    by_event: dict = {}
    for r in rows:
        key = r["event_id"] or f"{r['provider']}:{r['provider_event_key']}"
        by_event.setdefault(key, {"event_id": r["event_id"], "provider": r["provider"], "rows": []})["rows"].append(r)

    events = {}
    for key, ev in by_event.items():
        events[key] = {**_event_info(conn, ev["event_id"]), "sources": ["scoresheets"],
                       "provider": ev["provider"], "divisions": _group_divisions(ev["rows"]),
                       "registry_results": []}

    if include_registry:
        if registry["status"] == "found":
            rec = registry["record"]
            for p in rec["placements"]:
                key = p.get("catalog_event_id") or f"registry:{p['registry_event'].get('id')}:{p['registry_event'].get('month')}"
                ev = events.get(key)
                if ev is None:
                    ev = events[key] = {**_event_info(conn, p.get("catalog_event_id")), "sources": [],
                                        "provider": None, "divisions": [], "registry_results": []}
                    if not ev.get("event_name"):
                        ev["event_name"] = p["registry_event"].get("name")
                        ev["start_date"] = p["registry_event"].get("month")
                if "wsdc_registry" not in ev["sources"]:
                    ev["sources"].append("wsdc_registry")
                ev["registry_results"].append({
                    "role": p["role"], "division": p["division"], "division_abbr": p["division_abbr"],
                    "result": p["result"], "points": p["points"]})
            registry = {"status": "found", "wsdc_id": rec["wsdc_id"], "first_name": rec["first_name"],
                        "last_name": rec["last_name"], "primary_role": rec["primary_role"],
                        "level_allowed": rec["level_allowed"], "primary": rec["primary"],
                        "secondary": rec["secondary"]}

    for ev in events.values():
        wc = conn.execute("SELECT other_links FROM website_checks WHERE event_id=?",
                          (ev.get("event_id"),)).fetchone() if ev.get("event_id") else None
        ev["website_results_links"] = json.loads(wc["other_links"]) if wc else []
    items = list(events.values())
    if year:
        items = [e for e in items if (e.get("start_date") or "").startswith(str(year))]
    items.sort(key=lambda e: e.get("start_date") or "", reverse=True)
    return {
        "name": name, "normalized_name": norm, "total_events": len(items), "events": items,
        "registry": registry, "coverage": index_status(conn),
        "note": "Score-sheet matches use the exact name as printed on sheets (case and accents ignored). "
                "Registry results cover only point-earning finals.",
    }
