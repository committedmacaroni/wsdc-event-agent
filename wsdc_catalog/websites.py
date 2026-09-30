"""Find where an event's results are published by reading the event's own website.

For catalog events with a website (from the WSDC calendar or registry), fetch the home page
and up to three linked pages that look like results pages ("results", "scores", ...), and
collect links that point at supported scoring providers:
  - eepro.com/results/<slug>/...        -> Event Express Pro (event discovered via its year index)
  - scoring.dance/<locale>/events/<n>   -> scoring.dance (results index fetched directly)
Other results links (PDFs, the event's own pages) are recorded so Replit can show them,
but can't be searched by name.

Year check: event websites often keep linking last year's results, so a provider event is
attached to a catalog occurrence only when their dates agree; otherwise it's matched or
created as its own occurrence by date.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from . import eepro, scoringdance
from .db import iso, utcnow
from .enrichment import import_external_event
from .fetcher import fetch_html
from .scoresheets import discover_eepro_year, index_provider_events

RECHECK_AFTER = timedelta(days=7)
MAX_RESULT_PAGES = 3
_RESULTS_WORDS = re.compile(r"\b(results?|scores?|scoring|score ?sheets?|standings|placements)\b", re.I)
_EEPRO_RE = re.compile(r"eepro\.com/results/([^/?#]+)/", re.I)
_OTHER_RESULT_RE = re.compile(r"(results?|scores?|scoring|score ?sheets?)", re.I)
DATE_TOLERANCE_DAYS = 10


def _links(html: str, base: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html or "", "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        out.append((a.get_text(" ", strip=True), urljoin(base, href)))
    return out


def scan_website(url: str, *, fetch=fetch_html, delay: float = 0.5) -> dict:
    """-> {"eepro": {slug}, "scoring_dance": {number}, "other": [(text, url)], "pages": [...]}"""
    found = {"eepro": set(), "scoring_dance": set(), "other": [], "pages": [url]}
    home = fetch(url)
    links = _links(home, url)
    host = urlparse(url).netloc.lower().removeprefix("www.")
    result_pages = []
    for text, href in links:
        h = urlparse(href).netloc.lower().removeprefix("www.")
        if h == host and (_RESULTS_WORDS.search(text) or _RESULTS_WORDS.search(urlparse(href).path)) \
                and href not in result_pages and not href.lower().endswith(".pdf"):
            result_pages.append(href)
    for page in result_pages[:MAX_RESULT_PAGES]:
        if delay:
            time.sleep(delay)
        try:
            links += _links(fetch(page), page)
            found["pages"].append(page)
        except Exception:  # noqa: BLE001 - a broken sub-page shouldn't stop the scan
            continue
    seen_other = set(found["pages"])  # pages we scanned are navigation, not results to list
    for text, href in links:
        if (m := _EEPRO_RE.search(href)):
            found["eepro"].add(m.group(1))
        elif (n := scoringdance.event_number(href)):
            found["scoring_dance"].add(n)
        elif (_OTHER_RESULT_RE.search(text) or href.lower().endswith(".pdf") and _OTHER_RESULT_RE.search(href)) \
                and href not in seen_other and urlparse(href).netloc:
            seen_other.add(href)
            found["other"].append((text, href))
    return found


def _dates_agree(ev, provider_start: date | None) -> bool:
    if not provider_start or not ev["start_date"]:
        return False
    return abs((date.fromisoformat(ev["start_date"]) - provider_start).days) <= DATE_TOLERANCE_DAYS


def add_scoring_dance_event(conn, number: str, *, fetch=fetch_html, now=None, catalog_event=None) -> dict:
    """Read a scoring.dance event's results index and register it as a provider event."""
    now = now or utcnow()
    ts = iso(now)
    info = scoringdance.parse_results_index(fetch(scoringdance.results_index_url(number)), number)
    if not info["links"]:
        return {"number": number, "status": "no_results", "event_id": None}
    if info["date"] is None or not info["name"]:
        # The round pages reliably state "... during <Event> ... at MM/DD/YYYY": use the first one.
        try:
            first = scoringdance.parse_round_meta(fetch(info["links"][0][1]))
            info["date"] = info["date"] or first["date"]
            info["name"] = info["name"] or first["event_name"]
        except Exception:  # noqa: BLE001 - registration still proceeds without a date
            pass
    event_id = None
    if catalog_event is not None and _dates_agree(catalog_event, info["date"]):
        event_id = catalog_event["id"]
    elif info["date"] and info["name"]:
        linked = import_external_event(conn, {
            "source": scoringdance.PROVIDER, "name": info["name"], "start_date": info["date"].isoformat(),
            "external_id": number, "url": scoringdance.results_index_url(number)}, now=now)
        event_id = linked["event_id"]
    conn.execute(
        "INSERT INTO provider_events (provider, provider_key, event_id, name, start_date, end_date, index_url, "
        "links, fetched_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(provider, provider_key) DO UPDATE SET "
        "event_id=COALESCE(excluded.event_id, provider_events.event_id), name=excluded.name, "
        "start_date=excluded.start_date, links=excluded.links, fetched_at=excluded.fetched_at",
        (scoringdance.PROVIDER, str(number), event_id, info["name"],
         info["date"].isoformat() if info["date"] else None, None, scoringdance.results_index_url(number),
         json.dumps(info["links"]), ts))
    return {"number": number, "status": "registered", "event_id": event_id, "name": info["name"],
            "rounds": len(info["links"])}


def _attach_eepro(conn, slug: str, ev, *, fetch, now) -> dict:
    pe = conn.execute("SELECT * FROM provider_events WHERE provider=? AND provider_key=?",
                      (eepro.PROVIDER, slug)).fetchone()
    if pe is None:
        m = re.search(r"(20\d{2})", slug)
        if m:
            discover_eepro_year(conn, int(m.group(1)), fetch=fetch, now=now)
            pe = conn.execute("SELECT * FROM provider_events WHERE provider=? AND provider_key=?",
                              (eepro.PROVIDER, slug)).fetchone()
    if pe is None:
        return {"slug": slug, "status": "not_on_eepro_index"}
    if pe["event_id"] is None and _dates_agree(ev, date.fromisoformat(pe["start_date"]) if pe["start_date"] else None):
        conn.execute("UPDATE provider_events SET event_id=? WHERE provider=? AND provider_key=?",
                     (ev["id"], eepro.PROVIDER, slug))
    return {"slug": slug, "status": "registered", "event_id": pe["event_id"] or ev["id"]}


def discover_from_websites(conn, *, fetch=fetch_html, now: datetime | None = None, limit: int = 25,
                           event_ids: list[str] | None = None, index: bool = True,
                           delay: float = 0.5) -> dict:
    """Scan event websites for results links, register what's found, and index new sheets.

    Scans events whose website hasn't been checked in the last 7 days, most recently finished
    first (upcoming events have no results yet and are skipped).
    """
    now = now or utcnow()
    ts, today = iso(now), now.date().isoformat()
    if event_ids:
        ph = ",".join("?" * len(event_ids))
        events = conn.execute(f"SELECT * FROM events WHERE id IN ({ph}) AND website_url IS NOT NULL",
                              event_ids).fetchall()
    else:
        cutoff = iso(now - RECHECK_AFTER)
        events = conn.execute(
            "SELECT e.* FROM events e LEFT JOIN website_checks w ON w.event_id = e.id "
            "WHERE e.website_url IS NOT NULL AND e.website_url != '' AND e.start_date <= ? "
            "AND (w.checked_at IS NULL OR w.checked_at < ?) ORDER BY e.start_date DESC LIMIT ?",
            (today, cutoff, limit)).fetchall()
    events = [e for e in events if not re.search(r"(eepro\.com|scoring\.dance)", e["website_url"] or "", re.I)]
    report = {"events_checked": 0, "provider_events_found": 0, "other_results_links": 0,
              "errors": [], "events": []}
    for ev in events:
        report["events_checked"] += 1
        entry = {"event_id": ev["id"], "name": ev["name"], "website": ev["website_url"], "found": []}
        try:
            found = scan_website(ev["website_url"], fetch=fetch, delay=delay)
            for slug in sorted(found["eepro"]):
                entry["found"].append({"provider": "eepro", **_attach_eepro(conn, slug, ev, fetch=fetch, now=now)})
            for number in sorted(found["scoring_dance"]):
                entry["found"].append({"provider": "scoring_dance",
                                       **add_scoring_dance_event(conn, number, fetch=fetch, now=now,
                                                                 catalog_event=ev)})
            report["provider_events_found"] += len(entry["found"])
            report["other_results_links"] += len(found["other"])
            entry["other_results_links"] = [{"text": t, "url": u} for t, u in found["other"][:20]]
            conn.execute(
                "INSERT INTO website_checks (event_id, website_url, checked_at, status, provider_links, other_links) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET website_url=excluded.website_url, "
                "checked_at=excluded.checked_at, status=excluded.status, provider_links=excluded.provider_links, "
                "other_links=excluded.other_links, error=NULL",
                (ev["id"], ev["website_url"], ts, "found" if entry["found"] or found["other"] else "nothing_found",
                 json.dumps(entry["found"]), json.dumps(entry["other_results_links"])))
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"{ev['name']} ({ev['website_url']}): {e}")
            conn.execute(
                "INSERT INTO website_checks (event_id, website_url, checked_at, status, error) VALUES (?,?,?, 'error', ?) "
                "ON CONFLICT(event_id) DO UPDATE SET checked_at=excluded.checked_at, status='error', "
                "error=excluded.error", (ev["id"], ev["website_url"], ts, str(e)[:500]))
        report["events"].append(entry)
        if delay:
            time.sleep(delay)
    if index:
        report["indexing"] = index_provider_events(conn, provider=scoringdance.PROVIDER, fetch=fetch, now=now,
                                                   delay=delay)
    return report


# ------------------------------------------------------------------ scoring.dance bulk discovery
SD_MISS_LIMIT = 40          # stop an open-ended scan after this many consecutive missing event numbers
SD_RECENT_DAYS = 30         # re-read round lists for events this recent (new rounds get posted)


def scan_scoring_dance(conn, *, start: int | None = None, end: int | None = None, fetch=fetch_html,
                       now: datetime | None = None, delay: float = 1.0, index: bool = True) -> dict:
    """Walk scoring.dance event numbers and register every event that has results.

    - Backfill: start=1 (and no end) walks forward until SD_MISS_LIMIT numbers in a row don't exist.
    - Daily (no start/end): re-checks the last 30 numbers known, events from the last 30 days, then
      continues past the highest known number until the miss limit.
    Already-registered events older than 30 days are skipped, so re-running is cheap.
    """
    now = now or utcnow()
    known = {int(r["provider_key"]): r for r in conn.execute(
        "SELECT provider_key, start_date, fetched_at FROM provider_events WHERE provider=?",
        (scoringdance.PROVIDER,)) if str(r["provider_key"]).isdigit()}
    recent_cut = (now.date() - timedelta(days=SD_RECENT_DAYS)).isoformat()
    if start is None:
        top = max(known) if known else 0
        start = max(1, top - 30)
        recheck = sorted(n for n, r in known.items() if (r["start_date"] or "") >= recent_cut and n < start)
    else:
        recheck = []
    report = {"checked": 0, "registered": 0, "no_results": 0, "missing": 0, "skipped_known": 0,
              "highest_found": max(known) if known else None, "errors": [], "events": []}

    def visit(n: int) -> bool:
        """Returns True if the event number exists."""
        r = known.get(n)
        if r and (r["start_date"] or "9999") < recent_cut:
            report["skipped_known"] += 1
            return True
        report["checked"] += 1
        try:
            reg = add_scoring_dance_event(conn, str(n), fetch=fetch, now=now)
        except Exception as e:  # noqa: BLE001 - 404s and network errors count as "missing"
            report["missing"] += 1
            if "404" not in str(e):
                report["errors"].append(f"{n}: {e}")
            return False
        finally:
            if delay:
                time.sleep(delay)
        if reg["status"] == "registered":
            report["registered"] += 1
            report["events"].append({"number": n, "name": reg.get("name"), "event_id": reg.get("event_id"),
                                     "rounds": reg.get("rounds")})
            report["highest_found"] = max(report["highest_found"] or 0, n)
        else:
            report["no_results"] += 1
        return True

    for n in recheck:
        visit(n)
    n, misses = start, 0
    while (end is None and misses < SD_MISS_LIMIT) or (end is not None and n <= end):
        misses = 0 if visit(n) else misses + 1
        n += 1
    report["scanned_range"] = [start, n - 1]
    if index:
        report["indexing"] = index_provider_events(conn, provider=scoringdance.PROVIDER, fetch=fetch, now=now,
                                                   delay=delay)
    return report
