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
PARSER_VERSION = 1
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


def parse_sheet(html: str) -> dict:
    soup = BeautifulSoup(html or "", "lxml")
    h1s = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    title = next((h for h in h1s if " - " in h), h1s[0] if h1s else "")
    round_title = title.split(" - ")[0]
    round_title = re.sub(r"\s+results?$", "", round_title, flags=re.I).strip()
    event_title = title.split(" - ", 1)[1].strip() if " - " in title else None
    info = describe_title(round_title)
    sections, errors = [], []
    tables = soup.find_all("table")
    callback_tables = []
    for t in tables:
        rows = t.find_all("tr")
        if not rows:
            continue
        header = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
        if header and header[0].lower().startswith("bib") and "Σ" in header:
            callback_tables.append((header, rows[1:]))
    if not callback_tables and tables:
        errors.append(f"unrecognized table format in {round_title!r} (finals layout not yet supported)")
    roles = ["leader", "follower"] if len(callback_tables) == 2 else [info["role"]] * len(callback_tables)
    for (header, rows), role in zip(callback_tables, roles):
        sigma = header.index("Σ")
        judge_idx = [i for i in range(2, sigma) if _INITIALS.match(header[i] or "")]
        judges = [header[i] for i in judge_idx]
        entries = []
        for tr in rows:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) <= sigma or not cells[1]:
                continue
            marks = {}
            for i, j in zip(judge_idx, judges):
                raw = (cells[i] or "").lower().replace(" ", "")
                marks[j] = _MARKS.get(raw, cells[i] or None)
            entries.append({"role": role, "name": cells[1], "partner": None, "bib": cells[0],
                            "rank": None, "place": None, "marks": marks,
                            "counts": None, "score": _num(cells[sigma]),
                            "advanced": None, "alternate": None})
        # rank by score, highest first (the page lists rows in that order)
        for i, e in enumerate(entries, 1):
            e["rank"] = i
        sections.append({"title": round_title + (f" ({role}s)" if role and len(callback_tables) == 2 else ""),
                         "role": role, "round": info["round"], "division": info["division"],
                         "competed": len(entries), "judges": judges, "entries": entries})
    return {"event_title": event_title, "sections": [s for s in sections if s["entries"]], "errors": errors}
