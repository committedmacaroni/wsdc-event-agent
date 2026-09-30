"""One call for Replit: a dancer's name in, every event they appear in out.

Combines two sources:
  - the score-sheet index (every round, every indexed event), searched by exact name
  - the WSDC points registry (point-earning results at any event, including events
    whose score sheets aren't indexed), when the name matches exactly one registry dancer
"""
from __future__ import annotations

import json

from .judges import alias_map
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
                    "role": _role(p["role"]), "division": p["division"], "division_abbr": p["division_abbr"],
                    "result": p["result"], "points": p["points"]})
            registry = {"status": "found", "wsdc_id": rec["wsdc_id"], "first_name": rec["first_name"],
                        "last_name": rec["last_name"], "primary_role": _role(rec["primary_role"]),
                        "level_allowed": rec["level_allowed"], "primary": rec["primary"],
                        "secondary": rec["secondary"], "_record": rec}

    for ev in events.values():
        wc = conn.execute("SELECT other_links FROM website_checks WHERE event_id=?",
                          (ev.get("event_id"),)).fetchone() if ev.get("event_id") else None
        ev["website_results_links"] = json.loads(wc["other_links"]) if wc else []
    items = list(events.values())
    if year:
        items = [e for e in items if (e.get("start_date") or "").startswith(str(year))]
    items.sort(key=lambda e: e.get("start_date") or "", reverse=True)
    rec = registry.pop("_record", None) if isinstance(registry, dict) else None
    return {
        "name": name, "normalized_name": norm, "total_events": len(items), "events": items,
        "roles": _roles(items, rec),
        "summary": {**_summary(items, registry), "by_role": {
            role: _summary(_filter_role(items, role), registry, points_role=role)
            for role in _competed_roles(items)}},
        "progress": _progress(items),
        "wsdc_points": _points(items),
        "judges": _judges(items, alias_map(conn)),
        "registry": registry, "coverage": index_status(conn),
        "note": "Score-sheet matches use the exact name as printed on sheets (case and accents ignored). "
                "Registry results cover only point-earning finals.",
    }


# ------------------------------------------------------------------ dashboard-ready views
def _iter_rounds(items):
    for ev in items:
        for d in ev.get("divisions", []):
            for r in d["rounds"]:
                yield ev, d, r


def _progress(items) -> dict:
    """Chart-ready series. Callback rounds (prelims/quarters/semis) and finals are kept apart."""
    callbacks, finals = [], []
    for ev, d, r in _iter_rounds(items):
        base = {"date": ev.get("start_date"), "date_precision": ev.get("date_precision"),
                "event_id": ev.get("event_id"), "event_name": ev.get("event_name"),
                "division": d["division"], "role": d["role"], "round": r["round"]}
        if r["is_final"]:
            finals.append({**base, "place": r["place"], "partner": r["partner"],
                           "judge_placements": [m for m in r["mark_details"] if m.get("placement") is not None]})
        elif r.get("callback_pct") is not None:
            callbacks.append({**base, "callback_pct": r["callback_pct"], "callback_points": r["callback_points"],
                              "callback_max": r["callback_max"], "yes": r["yes"], "alt": r["alt"], "no": r["no"],
                              "advanced": r["advanced"], "rank": r["rank"], "competed": r["competed"]})
    for f in finals:
        f.update(source="scoresheets", result_label=_result_label(f["place"]), points=None)
    for ev in items:
        for rr in ev.get("registry_results", []):
            match = next((f for f in finals if f["event_id"] and f["event_id"] == ev.get("event_id")
                          and (f["role"] or "") == (rr["role"] or "")
                          and _same_division(f["division"], rr["division"])), None)
            if match:
                match["points"] = rr["points"]
                match["source"] = "scoresheets+wsdc_registry"
                continue
            code = str(rr["result"] or "").upper()
            finals.append({"date": ev.get("start_date"), "date_precision": ev.get("date_precision"),
                           "event_id": ev.get("event_id"), "event_name": ev.get("event_name"),
                           "division": rr["division"], "role": rr["role"], "round": "finals",
                           "place": int(code) if code.isdigit() else None,
                           "result_label": _result_label(code), "points": rr["points"], "partner": None,
                           "judge_placements": [], "source": "wsdc_registry"})
    key = lambda x: (x["date"] or "", {"prelims": 0, "quarters": 1, "semis": 2}.get(x["round"], 3))
    return {"callback_rounds": sorted(callbacks, key=key), "finals": sorted(finals, key=key),
            "note": "callback_pct = callback points / (10 x judges marking): Yes=10, Alt1=4.5, Alt2=4.3, "
                    "Alt3=4.2, No=0. Comparable across events with different panel sizes."}


def _judges(items, identities: dict | None = None) -> list[dict]:
    """One entry per judge (name variants resolved to one person) with every mark they gave."""
    identities = identities or {}
    by: dict = {}
    for ev, d, r in _iter_rounds(items):
        for m in r["mark_details"]:
            if not m.get("judge"):
                continue
            ident = identities.get(normalize_text(m["judge"]))
            key = ident["judge_key"] if ident else normalize_text(m["judge"])
            j = by.setdefault(key, {
                "judge": ident["judge_name"] if ident else m["judge"], "events": set(), "callback_marks": 0,
                "yes": 0, "alt": 0, "no": 0, "points_total": 0.0, "finals_placements": [], "history": [],
                "names": {}, "needs_review": False})
            j["names"].setdefault(m["judge"], {"name": m["judge"],
                                               "method": ident["method"] if ident else "exact",
                                               "confidence": ident["confidence"] if ident else "high",
                                               "reason": ident["reason"] if ident else None})
            if ident and ident["method"] == "needs_review":
                j["needs_review"] = True
            j["events"].add(ev.get("event_id") or ev.get("event_name"))
            h = {"date": ev.get("start_date"), "event_id": ev.get("event_id"), "event_name": ev.get("event_name"),
                 "division": d["division"], "role": d["role"], "round": r["round"]}
            if r["is_final"]:
                if m.get("placement") is not None:
                    j["finals_placements"].append(m["placement"])
                j["history"].append({**h, "placement": m.get("placement"), "final_place": r["place"]})
            elif m.get("points") is not None:
                j["callback_marks"] += 1
                j[m["kind"]] = j.get(m["kind"], 0) + 1
                j["points_total"] += m["points"]
                j["history"].append({**h, "mark": m["mark"], "label": m["label"], "points": m["points"]})
    out = []
    for j in by.values():
        n = j["callback_marks"]
        fp = j["finals_placements"]
        out.append({
            "judge": j["judge"], "events_judged": len(j["events"]), "callback_marks": n,
            "yes": j["yes"], "alt": j["alt"], "no": j["no"],
            "yes_rate": round(100 * j["yes"] / n, 1) if n else None,
            "avg_points": round(j["points_total"] / n, 2) if n else None,
            "finals_judged": len(fp), "avg_finals_placement": round(sum(fp) / len(fp), 2) if fp else None,
            "history": sorted(j["history"], key=lambda x: x["date"] or ""),
            "listed_as": sorted(j["names"].values(), key=lambda x: x["name"]),
            "needs_review": j["needs_review"],
        })
    return sorted(out, key=lambda x: (-x["events_judged"], x["judge"]))


def _same_division(a, b) -> bool:
    return normalize_text(a or "") == normalize_text(b or "")


def _summary(items, registry, points_role=None) -> dict:
    rounds = list(_iter_rounds(items))
    callbacks = [r for _, _, r in rounds if not r["is_final"] and r.get("callback_pct") is not None]
    decided = [r for r in callbacks if r["advanced"] is not None]
    finals = _progress(items)["finals"]  # score-sheet finals + registry results, already merged
    places = [f["place"] for f in finals if f["place"]]
    have_callbacks = bool(callbacks)
    return {
        "events_competed": len(items),
        "events_with_scoresheets": sum(1 for e in items if e.get("divisions")),
        "finals_made": len(finals),
        "events_with_finals": len({f["event_id"] or f["event_name"] for f in finals}),
        "best_final_place": min(places) if places else None,
        "best_final_label": _result_label(min(places)) if places else ("Finalist" if finals else None),
        "callback_data_available": have_callbacks,
        "callback_rounds": len(callbacks),
        "callback_rounds_advanced": sum(1 for r in decided if r["advanced"]),
        "advancement_rate": round(100 * sum(1 for r in decided if r["advanced"]) / len(decided), 1) if decided else None,
        "avg_callback_pct": round(sum(r["callback_pct"] for r in callbacks) / len(callbacks), 1) if callbacks else None,
        "callback_note": None if have_callbacks else
            "No prelim/semi score sheets indexed for this dancer yet, so callback stats aren't available.",
        "wsdc_points": sum((p.get("points") or 0) for e in items for p in e.get("registry_results", [])
                           if points_role is None or p.get("role") == points_role) or None,
        "level_allowed": registry.get("level_allowed") if isinstance(registry, dict) and points_role is None else None,
    }


def _result_label(code) -> str | None:
    """WSDC registry result codes: '1'..'5' = placement, 'F' = finalist (made the final, unplaced)."""
    if code is None:
        return None
    c = str(code).strip().upper()
    if c.isdigit():
        n = int(c)
        return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"
    return {"F": "Finalist"}.get(c, c)


def _points(items) -> dict:
    """WSDC points awarded, one row per result, plus totals by division and role."""
    awards = []
    for ev in items:
        for r in ev.get("registry_results", []):
            awards.append({
                "date": ev.get("start_date"), "event_id": ev.get("event_id"), "event_name": ev.get("event_name"),
                "city": ev.get("city"), "country": ev.get("country"),
                "division": r["division"], "division_abbr": r["division_abbr"], "role": r["role"],
                "result": r["result"], "result_label": _result_label(r["result"]), "points": r["points"] or 0})
    awards.sort(key=lambda a: a["date"] or "", reverse=True)
    totals = {}
    for a in awards:
        t = totals.setdefault((a["division"], a["role"]), {"division": a["division"],
                                                           "division_abbr": a["division_abbr"],
                                                           "role": a["role"], "points": 0, "results": 0})
        t["points"] += a["points"]
        t["results"] += 1
    return {"total": sum(a["points"] for a in awards), "awards": awards,
            "by_division": sorted(totals.values(), key=lambda t: (-t["points"], t["division"] or ""))}


# ------------------------------------------------------------------ roles
def _role(value) -> str | None:
    """'Follower' / 'follower' / 'FOLLOWER' -> 'follower'."""
    v = normalize_text(value or "")
    return {"leader": "leader", "lead": "leader", "follower": "follower", "follow": "follower"}.get(v, v or None)


def _competed_roles(items) -> list[str]:
    seen = []
    for ev in items:
        for d in ev.get("divisions", []):
            if d.get("role") and d["role"] not in seen:
                seen.append(d["role"])
        for r in ev.get("registry_results", []):
            if r.get("role") and r["role"] not in seen:
                seen.append(r["role"])
    return sorted(seen)


def _filter_role(items, role) -> list[dict]:
    out = []
    for ev in items:
        divs = [d for d in ev.get("divisions", []) if d.get("role") == role]
        regs = [r for r in ev.get("registry_results", []) if r.get("role") == role]
        if divs or regs:
            out.append({**ev, "divisions": divs, "registry_results": regs})
    return out


def _roles(items, rec) -> dict:
    """Primary/secondary role with levels (from the WSDC registry when available)."""
    points = {}
    for ev in items:
        for r in ev.get("registry_results", []):
            points[r["role"]] = points.get(r["role"], 0) + (r.get("points") or 0)
    competed = _competed_roles(items)

    def block(role, level_allowed, level_required, highest, highest_pts, recommended=None, rule=None):
        if not role:
            return None
        return {"role": role, "role_label": role.capitalize(), "level_allowed": level_allowed,
                "level_required": level_required, "level_recommended": recommended,
                "highest_level": highest, "highest_level_points": highest_pts,
                "wsdc_points": points.get(role, 0), "rule": rule}

    if rec:
        p_role, s_role = _role(rec.get("primary_role")), _role(rec.get("secondary_role"))
        return {
            "source": "wsdc_registry", "competed_roles": competed,
            "primary": block(p_role, rec.get("level_allowed"), rec.get("level_required"),
                             rec["primary"].get("highest_level"), rec["primary"].get("highest_level_points")),
            "secondary": block(s_role, rec.get("secondary_level_allowed"), rec.get("secondary_level_required"),
                               rec["secondary"].get("highest_level"), rec["secondary"].get("highest_level_points"),
                               rec.get("secondary_level_recommended"), rec.get("secondary_rule")),
        }
    # No registry record: infer primary role from where the dancer has the most results.
    counts = {r: 0 for r in competed}
    for ev in items:
        for d in ev.get("divisions", []):
            if d.get("role"):
                counts[d["role"]] += len(d["rounds"])
    ordered = sorted(counts, key=lambda r: -counts[r])
    return {"source": "scoresheets" if ordered else None, "competed_roles": competed,
            "primary": block(ordered[0], None, None, None, None) if ordered else None,
            "secondary": block(ordered[1], None, None, None, None) if len(ordered) > 1 else None}
