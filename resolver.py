"""Resolve an event_id or loose event metadata to a catalog event + matching context.

Used by /find-results before any scoring-provider lookup.
"""
from __future__ import annotations

from datetime import date

from .normalize import is_short_alias, normalize_country_filter, normalize_event_name, normalize_text
from .queries import aliases_for, get_event

RESOLVED_MIN = 0.7
FUZZY_NAME_MIN = 0.6  # token Jaccard needed for a non-exact name to count at all
CANDIDATE_MIN = 0.3
RESOLVED_MARGIN = 0.15
MAX_CANDIDATES = 5


def build_match_context(event: dict) -> dict:
    """Everything a scoring-provider matcher needs, derived from the catalog."""
    names = [event["canonical_name"], event["name"], *event.get("aliases", [])]
    terms = list(dict.fromkeys(n for n in names if n))
    if event.get("year"):
        terms.append(f"{event['canonical_name']} {event['year']}")
    return {
        "event_id": event["id"], "series_id": event["series_id"],
        "canonical_name": event["canonical_name"], "occurrence_name": event["name"],
        "aliases": event.get("aliases", []), "search_terms": terms,
        "start_date": event["start_date"], "end_date": event["end_date"], "year": event["year"],
        "city": event["city"], "region": event["region"], "country": event["country"],
    }


def _tokens(s: str) -> set[str]:
    return set(normalize_text(s).split())


def _score(conn, row, meta: dict) -> float:
    name_norm = normalize_event_name(meta.get("name") or "")
    series_norm = row["series_norm"]
    alias_norms = [a["normalized_alias"] for a in aliases_for(conn, row["series_id"])]
    score = 0.0
    if name_norm and name_norm in (series_norm, normalize_event_name(row["name"])):
        score += 0.6
    elif name_norm in alias_norms:
        score += 0.3 if is_short_alias(name_norm) else 0.6
    else:
        best = 0.0
        for cand in [series_norm, *alias_norms]:
            a, b = _tokens(name_norm), _tokens(cand)
            if a and b:
                best = max(best, len(a & b) / len(a | b))
        if best < FUZZY_NAME_MIN:
            return 0.0  # name doesn't match: dates/location alone never make a candidate
        score += 0.5 * best
    start = meta.get("start_date")
    if start and row["start_date"]:
        try:
            diff = abs((date.fromisoformat(start) - date.fromisoformat(row["start_date"])).days)
            score += 0.3 if diff <= 3 else 0.15 if diff <= 14 else -0.2 if diff > 60 else 0
        except ValueError:
            pass
    elif meta.get("year") and row["year"]:
        score += 0.15 if int(meta["year"]) == row["year"] else -0.3
    if meta.get("country") and row["country"]:
        score += 0.1 if normalize_country_filter(meta["country"]) == row["country"] else -0.3
    if meta.get("city") and row["city"] and normalize_text(meta["city"]) == normalize_text(row["city"]):
        score += 0.05
    return round(min(score, 1.0), 3)


def resolve_event(conn, event_id: str | None = None, metadata: dict | None = None) -> dict:
    if event_id:
        ev = get_event(conn, event_id)
        if not ev:
            return {"status": "not_found", "event_id": event_id, "candidates": []}
        return {"status": "resolved", "confidence": 1.0, "event": ev,
                "match_context": build_match_context(ev), "candidates": []}

    meta = metadata or {}
    name_norm = normalize_event_name(meta.get("name") or "")
    if not name_norm:
        return {"status": "not_found", "candidates": [], "reason": "no event_id or name supplied"}
    tokens = [t for t in name_norm.split() if len(t) > 2] or name_norm.split()
    like = " OR ".join(["s.normalized_name LIKE ?", "e.normalized_name LIKE ?",
                        "EXISTS (SELECT 1 FROM event_aliases a WHERE a.series_id=e.series_id "
                        "AND a.normalized_alias LIKE ?)"] * len(tokens))
    args = [f"%{t}%" for t in tokens for _ in range(3)]
    rows = conn.execute(
        f"SELECT e.*, s.normalized_name AS series_norm FROM events e "
        f"JOIN event_series s ON s.id=e.series_id WHERE {like}", args).fetchall()
    scored = sorted(((_score(conn, r, meta), r) for r in rows), key=lambda t: (-t[0], t[1]["start_date"] or ""))
    scored = [(s, r) for s, r in scored if s >= CANDIDATE_MIN][:MAX_CANDIDATES]
    candidates = [{"event_id": r["id"], "name": r["name"], "start_date": r["start_date"],
                   "country": r["country"], "score": s} for s, r in scored]
    if not scored:
        return {"status": "not_found", "candidates": []}
    top = scored[0][0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if top >= RESOLVED_MIN and top - second >= RESOLVED_MARGIN:
        ev = get_event(conn, scored[0][1]["id"])
        return {"status": "resolved", "confidence": top, "event": ev,
                "match_context": build_match_context(ev), "candidates": candidates}
    return {"status": "ambiguous", "confidence": top, "candidates": candidates}


def prepare_find_results_request(conn, body: dict) -> dict:
    """Call at the top of /find-results.

    Returns {"ok": True, "match_context": ..., "catalog_status": ...} to proceed, or
    {"ok": False, "status_code": 404|409, "error": ...} to return to the caller.
    """
    if body.get("event_id"):
        res = resolve_event(conn, event_id=body["event_id"])
        if res["status"] != "resolved":
            return {"ok": False, "status_code": 404,
                    "error": {"code": "event_not_found", "message": f"unknown event_id {body['event_id']}"}}
        return {"ok": True, "catalog_status": "resolved", "match_context": res["match_context"]}
    meta = {k: body.get(k) for k in ("name", "start_date", "end_date", "year", "city", "country")}
    res = resolve_event(conn, metadata=meta)
    if res["status"] == "resolved":
        return {"ok": True, "catalog_status": "resolved", "confidence": res["confidence"],
                "match_context": res["match_context"]}
    if res["status"] == "ambiguous":
        return {"ok": False, "status_code": 409,
                "error": {"code": "ambiguous_event", "message": "metadata matches several catalog events; "
                          "resend with event_id", "candidates": res["candidates"]}}
    ctx = {"event_id": None, "canonical_name": meta.get("name"), "occurrence_name": meta.get("name"),
           "aliases": [], "search_terms": [meta["name"]] if meta.get("name") else [],
           "start_date": meta.get("start_date"), "end_date": meta.get("end_date"),
           "year": meta.get("year"), "city": meta.get("city"), "region": None,
           "country": normalize_country_filter(meta.get("country"))}
    return {"ok": True, "catalog_status": "uncatalogued", "match_context": ctx}
