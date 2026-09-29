"""scoring.dance results.

URL structure (observed via search results, 2026-09-29):
  event results index:  https://scoring.dance/<locale>/events/<event number>/results/
  one round:            https://scoring.dance/<locale>/events/<event number>/results/<round number>.html
We always use the enUS locale so marks read Yes/No/Alt1-3 (other locales translate them).

Round page (prelims confirmed; finals NOT yet seen):
  heading "Novice Jack&Jill prelim results - Paris Swing Classic 2025"
  text    "... during Paris Swing Classic 2025 in Paris France at 01/30/2025. Judges ..."
  tables  one per role (leaders first, then followers):
          Bib Number | <name> | | <judge initials>... | <head judge> | Σ | |
          246        | Lee Bartholomew | | Yes | Yes | Yes | | 30 | |
Finals tables are only parsed if they look like a callback table; otherwise the page is
reported as an unrecognized format so it can be captured and supported properly.
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .eepro import describe_title

PROVIDER = "scoring_dance"
PARSER_VERSION = 3  # 2: real prelim markup; 3: finals (couples, Placement column)
BASE = "https://scoring.dance"
_EVENT_RE = re.compile(r"scoring\.dance/(?:[a-z]{2}[A-Z]{2}/)?events/(\d+)", re.I)
_ROUND_RE = re.compile(r"/events/(\d+)/results/(\d+)\.html", re.I)
_DATE_RE = re.compile(r"\bat (\d{1,2})/(\d{1,2})/(\d{4})")
_MARKS = {"yes": "Y", "no": "N", "alt1": "A1", "alt2": "A2", "alt3": "A3", "alt": "A"}
_INITIALS = re.compile(r"^[A-ZÀ-Ý][A-Za-zÀ-ÿ]{0,4}$")


def event_number(url: str) -> str | None:
    m = _EVENT_RE.search(url or "")
    return m.group(1) if m else None


def results_index_url(number: str | int) -> str:
    return f"{BASE}/enUS/events/{number}/results/"


def to_en(url: str) -> str:
    return re.sub(r"scoring\.dance/[a-z]{2}[A-Z]{2}/", "scoring.dance/enUS/", url)


def parse_results_index(html: str, number: str) -> dict:
    """-> {"name", "date", "links": [(title, url)]} for one event's results index page."""
    soup = BeautifulSoup(html or "", "lxml")
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = to_en(urljoin(results_index_url(number), a["href"]))
        m = _ROUND_RE.search(urlparse(url).path)
        if m and m.group(1) == str(number) and url not in seen:
            seen.add(url)
            links.append((a.get_text(" ", strip=True), url))
    h1 = soup.find("h1")
    name = re.sub(r"\s+results?\s*$", "", h1.get_text(" ", strip=True), flags=re.I) if h1 else None
    dm = _DATE_RE.search(soup.get_text(" "))
    when = date(int(dm.group(3)), int(dm.group(1)), int(dm.group(2))) if dm else None
    return {"name": name, "date": when, "links": links}


def _num(text: str) -> float | None:
    try:
        return float(text.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def _cell_text(cell) -> str:
    """Visible text, or an image's alt text (the Bib Number header is an icon)."""
    t = cell.get_text(" ", strip=True)
    if not t:
        img = cell.find("img", alt=True)
        t = img["alt"].strip() if img else ""
    return t


_STATE_ADVANCED = {"CB"}  # data-state on each row; "CB" = callback (advanced)


def parse_sheet(html: str) -> dict:
    soup = BeautifulSoup(html or "", "lxml")
    h1s = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    title = next((h for h in h1s if " - " in h), h1s[0] if h1s else "")
    round_title = re.sub(r"\s+results?$", "", title.split(" - ")[0], flags=re.I).strip()
    event_title = title.split(" - ", 1)[1].strip() if " - " in title else None
    info = describe_title(round_title)
    sections, errors = [], []
    callback_tables, final_tables, other_tables = [], [], 0
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        if not rows:
            continue
        head_cells = rows[0].find_all(["th", "td"])
        header = [_cell_text(c) for c in head_cells]
        if header and header[0].lower().startswith("bib") and any(h in ("Σ", "∑") for h in header):
            callback_tables.append((head_cells, header, rows[1:]))
        elif header and header[0].lower().startswith("bib") and "placement" in [h.lower() for h in header]:
            final_tables.append((head_cells, header, rows[1:]))
        else:
            other_tables += 1
    for head_cells, header, rows in final_tables:
        sections.append(_parse_final_table(head_cells, header, rows, round_title, info))
    if not callback_tables and not final_tables and other_tables:
        errors.append(f"unrecognized table format in {round_title!r}"
                      + (" (finals layout not yet supported)" if info["round"] == "finals" else ""))
    roles = ["leader", "follower"] if len(callback_tables) == 2 else [info["role"]] * len(callback_tables)
    for (head_cells, header, rows), role in zip(callback_tables, roles):
        sigma = next(i for i, h in enumerate(header) if h in ("Σ", "∑"))
        judge_idx = [i for i in range(2, sigma) if header[i]]
        judges = [re.sub(r"\s*\((?:chief|head)\s*judge\)\s*$", "", head_cells[i].get("title") or header[i],
                         flags=re.I).strip() for i in judge_idx]
        entries = []
        for rank, tr in enumerate(rows, 1):
            tds = tr.find_all(["td", "th"])
            if len(tds) <= sigma:
                continue
            link = tds[1].find("a")
            name = (link.get_text(" ", strip=True) if link else _cell_text(tds[1])).strip()
            if not name:
                continue
            marks = {}
            for i, j in zip(judge_idx, judges):
                raw = _cell_text(tds[i])
                if raw:
                    marks[j] = _MARKS.get(raw.lower().replace(" ", ""), raw)
            state = (tr.get("data-state") or "").strip().upper() or None
            wsdc = link.get("data-wsdc") if link else None
            entries.append({"role": role, "name": name, "partner": None, "bib": _cell_text(tds[0]),
                            "rank": rank, "place": None, "marks": marks, "counts": None,
                            "score": _num(_cell_text(tds[sigma])),
                            "advanced": (state in _STATE_ADVANCED) if state else None,
                            "alternate": state if state and state.startswith("ALT") else None,
                            "wsdc_id": int(wsdc) if wsdc and wsdc.isdigit() else None,
                            "state": state})
        sections.append({"title": round_title + (f" ({role}s)" if role and len(callback_tables) == 2 else ""),
                         "role": role, "round": info["round"], "division": info["division"],
                         "competed": len(entries), "judges": judges, "entries": entries})
    return {"event_title": event_title, "sections": [s for s in sections if s["entries"]], "errors": errors}


def _name_and_id(cell):
    link = cell.find("a")
    name = (link.get_text(" ", strip=True) if link else _cell_text(cell)).strip()
    wsdc = link.get("data-wsdc") if link else None
    return name, (int(wsdc) if wsdc and wsdc.isdigit() else None)


def _parse_final_table(head_cells, header, rows, round_title, info) -> dict:
    """Finals: Bib | Leader | Follower | judge placements... | head judge | Placement ("1st")."""
    place_idx = [h.lower() for h in header].index("placement")
    judge_idx = [i for i in range(3, place_idx) if header[i]]
    judges = [re.sub(r"\s*\((?:chief|head)\s*judge\)\s*$", "", head_cells[i].get("title") or header[i],
                     flags=re.I).strip() for i in judge_idx]
    entries = []
    for tr in rows:
        tds = tr.find_all(["td", "th"])
        if len(tds) <= place_idx:
            continue
        leader, leader_id = _name_and_id(tds[1])
        follower, follower_id = _name_and_id(tds[2])
        if not leader and not follower:
            continue
        marks = {}
        for i, j in zip(judge_idx, judges):
            raw = _cell_text(tds[i])
            if raw:
                marks[j] = int(raw) if raw.isdigit() else raw
        pm = re.match(r"\s*(\d+)", _cell_text(tds[place_idx]))
        place = int(pm.group(1)) if pm else None
        base = {"rank": place, "place": place, "marks": marks, "counts": None, "score": None,
                "advanced": None, "alternate": None, "state": (tr.get("data-state") or "").strip() or None}
        bib = _cell_text(tds[0]) or None
        if leader:
            entries.append({**base, "role": "leader", "name": leader, "partner": follower or None,
                            "bib": bib, "wsdc_id": leader_id})
        if follower:
            entries.append({**base, "role": "follower", "name": follower, "partner": leader or None,
                            "bib": None, "wsdc_id": follower_id})
    return {"title": round_title, "role": None, "round": info["round"] or "finals",
            "division": info["division"], "competed": len({e["place"] for e in entries}) or None,
            "judges": judges, "entries": entries}
