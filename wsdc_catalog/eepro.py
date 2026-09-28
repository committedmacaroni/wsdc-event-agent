"""Event Express Pro (eepro.com) results: year index + score-sheet parser.

Formats observed on 2026-09-24 (https://eepro.com/results/2026/ and the SwingTime 2026 sheets):

Year index: event headers "September 10-13, 2026 - SwingTime" followed by links
  https://eepro.com/results/<slug>/<page>.html (J&J prelims, finals, strictly, ...).

Prelim/semi sheets, one section per division and round:
  "Jack & Jill Follower Novice Prelims - 66 competed"
  header: Count | Competitor | <judge>... | BIB | Counts (Y-A-N) | Sum | Promote | Alt
  row:    18 | Tanya Davis | Y | Y | A3 | N | 126 | 2-1-1 | 24.2 | X | ALT1
  (rows without a Count did not advance; couples divisions list "Leader and Follower")

Final sheets:
  "Division: Jack & Jill Novice Finals"
  header: Place | Competitor | <judge>... | BIB | Marks Sorted
  row:    1 | Josh Schroeder and Emily Madden | 1 | 1 | 4 | 5 | 2 | 321/172 | 1-1-2-4-5

The parser reads table cells when the page uses tables and falls back to whitespace
tokens otherwise; every row is parsed right-to-left, and the judge count comes from the
row itself (Y-A-N counts or sorted marks), so it doesn't depend on exact markup.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString

from .normalize import parse_date_range

PROVIDER = "eepro"
INDEX_URL = "https://eepro.com/results/{year}/"

_HEADER_RE = re.compile(
    r"^\s*(?P<date>[A-Z][a-z]{2,8}\.?\s+\d{1,2}(?:\s*[-\u2013]\s*(?:[A-Z][a-z]{2,8}\.?\s+)?\d{1,2})?,?\s+\d{4})"
    r"\s*[-\u2013\u2014]\s*(?P<name>.+?)\s*$")
_SLUG_RE = re.compile(r"/results/(?P<slug>[^/]+)/[^/]+\.(?:html?|pdf)$", re.IGNORECASE)
_TITLE_RE = re.compile(r"^\s*(?:Division:\s*)?(?P<title>.+?)(?:\s*-\s*(?P<n>\d+)\s+competed)?\s*$", re.IGNORECASE)
_PRELIM_MARK = re.compile(r"^(Y|N|A\d|ALT\d|-)$", re.IGNORECASE)
_COUNTS_RE = re.compile(r"^(\d+)-(\d+)-(\d+)$")
_SORTED_RE = re.compile(r"^\d+(?:-\d+)+$")
_ROUND_RE = re.compile(r"\b(prelims?|preliminar(?:y|ies)|quarter(?:final)?s?|semi(?:final)?s?|finals?)\b", re.I)
_DIVISIONS = [
    ("All Star", r"all[\s-]?stars?"), ("Champions", r"champions?"), ("Invitational", r"invitational"),
    ("Advanced", r"advanced"), ("Intermediate", r"intermediate"), ("Novice", r"novice"),
    ("Newcomer", r"newcomers?"), ("Masters", r"masters"), ("Sophisticated", r"sophisticated"),
    ("Juniors", r"juniors?"), ("Teens", r"teens?"), ("All-In", r"all[\s-]?in"), ("Open", r"open"),
]


# ------------------------------------------------------------------ year index
@dataclass
class IndexEvent:
    key: str
    name: str
    date_raw: str
    start: object = None
    end: object = None
    links: list = field(default_factory=list)  # [(title, url)]


def parse_index(html: str, base_url: str) -> tuple[list[IndexEvent], list[str]]:
    soup = BeautifulSoup(html or "", "lxml")
    events: dict[str, IndexEvent] = {}
    order: list[str] = []
    errors: list[str] = []
    current = None  # (date_raw, name)
    for node in soup.descendants:
        if isinstance(node, NavigableString):
            m = _HEADER_RE.match(str(node))
            if m and not (node.parent and node.parent.name == "a"):
                current = (m["date"], m["name"])
            continue
        if getattr(node, "name", None) != "a" or not node.get("href"):
            continue
        url = urljoin(base_url, node["href"])
        sm = _SLUG_RE.search(urlparse(url).path)
        if not sm or current is None:
            continue
        slug = sm["slug"]
        if slug not in events:
            ev = IndexEvent(key=slug, name=current[1], date_raw=current[0])
            try:
                ev.start, ev.end = parse_date_range(current[0])
            except ValueError as e:
                errors.append(f"{slug}: {e}")
            events[slug] = ev
            order.append(slug)
        title = node.get_text(" ", strip=True)
        if (title, url) not in events[slug].links:
            events[slug].links.append((title, url))
    return [events[k] for k in order], errors


# ------------------------------------------------------------------ sheet parsing
def _lines(html: str) -> list[list[str]]:
    """Page as lines of cells: table cells become separate cells; other lines are whitespace-split later."""
    soup = BeautifulSoup(html or "", "lxml")
    for t in soup(["script", "style", "head", "title"]):
        t.decompose()
    for cell in soup.find_all(["td", "th"]):
        cell.append("\t")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["tr", "li", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul"]):
        block.append("\n")
    out = []
    for raw in soup.get_text().split("\n"):
        if not raw.strip():
            continue
        if "\t" in raw:
            cells = [re.sub(r"\s+", " ", c).strip() for c in raw.split("\t")]
            while cells and not cells[-1]:
                cells.pop()
            out.append(cells)
        else:
            out.append([re.sub(r"\s+", " ", raw).strip()])
    return out


def _tokens(cells: list[str]) -> tuple[list[str], bool]:
    """(tokens, cell_mode). In cell mode the competitor name is a single token."""
    if len(cells) > 1:
        return [c for c in cells if c != ""], True
    return cells[0].split(), False


def describe_title(title: str) -> dict:
    t = title or ""
    role = "leader" if re.search(r"\bleaders?\b", t, re.I) else "follower" if re.search(r"\bfollowers?\b", t, re.I) else None
    rounds = _ROUND_RE.findall(t)
    rnd = None
    if rounds:
        r = rounds[-1].lower()
        rnd = ("prelims" if r.startswith("prelim") else "quarters" if r.startswith("quarter")
               else "semis" if r.startswith("semi") else "finals")
    division = next((name for name, pat in _DIVISIONS if re.search(rf"\b{pat}\b", t, re.I)), None)
    return {"role": role, "round": rnd, "division": division}


def _parse_prelim_row(tokens: list[str], cell_mode: bool) -> dict | None:
    t = list(tokens)
    alternate, advanced = None, False
    if t and re.fullmatch(r"ALT\d+", t[-1], re.I):
        alternate = t.pop().upper()
    if t and t[-1].upper() == "X":
        advanced = True
        t.pop()
    if len(t) < 5:
        return None
    try:
        score = float(t.pop())
    except ValueError:
        return None
    cm = _COUNTS_RE.match(t.pop())
    if not cm:
        return None
    judges = sum(int(x) for x in cm.groups())
    bib = t.pop()
    if judges == 0 or len(t) < judges + 1:
        return None
    marks = t[-judges:]
    if not all(_PRELIM_MARK.match(m) for m in marks):
        return None
    t = t[:-judges]
    rank = int(t.pop(0)) if t and t[0].isdigit() and len(t) > 1 else None
    name = " ".join(t).strip()
    if not name:
        return None
    return {"rank": rank, "name": name, "marks": [m.upper() for m in marks], "bib": bib,
            "counts": cm.group(0), "score": score, "advanced": advanced and alternate is None,
            "alternate": alternate}


def _parse_final_row(tokens: list[str], cell_mode: bool) -> dict | None:
    t = list(tokens)
    if len(t) < 4 or not _SORTED_RE.match(t[-1]):
        return None
    sorted_marks = t.pop()
    judges = len(sorted_marks.split("-"))
    bib = t.pop()
    if len(t) < judges + 1:
        return None
    marks = t[-judges:]
    if not all(m.isdigit() for m in marks):
        return None
    t = t[:-judges]
    place = int(t.pop(0)) if t and t[0].isdigit() and len(t) > 1 else None
    name = " ".join(t).strip()
    if not name:
        return None
    return {"place": place, "name": name, "marks": [int(m) for m in marks], "bib": bib,
            "counts": sorted_marks, "score": None, "advanced": None, "alternate": None}


def _split_couple(name: str) -> tuple[str, str] | None:
    parts = re.split(r"\s+(?:and|&)\s+", name, maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 and all(parts) else None


def parse_sheet(html: str) -> dict:
    """Returns {"event_title", "sections": [{title, division, round, role, competed, judges, entries}], "errors"}."""
    lines = _lines(html)
    sections, errors = [], []
    event_title = None
    section = None
    mode = None  # "prelim" | "final"
    for cells in lines:
        text = " ".join(cells)
        first = cells[0]
        if event_title is None and re.search(r"\b20\d{2}\b", text) and "competed" not in text \
                and not text.lower().startswith("division"):
            event_title = text
        tm = _TITLE_RE.match(text)
        if tm and (tm["n"] or text.lower().startswith("division:")) and len(cells) <= 2:
            section = {"title": tm["title"].strip(), **describe_title(tm["title"]),
                       "competed": int(tm["n"]) if tm["n"] else None, "judges": [], "entries": []}
            sections.append(section)
            mode = None
            continue
        toks, cell_mode = _tokens(cells)
        if toks and toks[0].lower() in ("count", "place") and "competitor" in text.lower():
            mode = "prelim" if toks[0].lower() == "count" else "final"
            if section is None:  # header without a title line
                section = {"title": None, "role": None, "round": None, "division": None,
                           "competed": None, "judges": [], "entries": []}
                sections.append(section)
            if cell_mode:
                try:
                    i, j = [c.lower() for c in toks].index("competitor"), [c.lower() for c in toks].index("bib")
                    section["judges"] = toks[i + 1:j]
                except ValueError:
                    pass
            continue
        if section is None or mode is None:
            continue
        row = (_parse_prelim_row if mode == "prelim" else _parse_final_row)(toks, cell_mode)
        if row is None:
            if re.match(r"^\d", text) and len(toks) > 4:
                errors.append(f"unparsed row in {section['title']!r}: {text[:120]}")
            continue
        if section["round"] is None:
            section["round"] = "finals" if mode == "final" else "prelims"
        section["entries"].extend(_expand(row, section))
    return {"event_title": event_title, "sections": [s for s in sections if s["entries"]], "errors": errors}


def _expand(row: dict, section: dict) -> list[dict]:
    """One entry per dancer: couples become a leader entry and a follower entry."""
    judges = section.get("judges") or []
    marks = dict(zip(judges, row["marks"])) if len(judges) == len(row["marks"]) else row["marks"]
    base = {k: row.get(k) for k in ("rank", "place", "counts", "score", "advanced", "alternate")}
    base["marks"] = marks
    couple = _split_couple(row["name"]) if section.get("role") is None else None
    if couple:
        bibs = row["bib"].split("/")
        lb, fb = (bibs + [None])[:2] if len(bibs) > 1 else (row["bib"], None)
        return [{**base, "role": "leader", "name": couple[0], "partner": couple[1], "bib": lb},
                {**base, "role": "follower", "name": couple[1], "partner": couple[0], "bib": fb}]
    return [{**base, "role": section.get("role"), "name": row["name"], "partner": None, "bib": row["bib"]}]
