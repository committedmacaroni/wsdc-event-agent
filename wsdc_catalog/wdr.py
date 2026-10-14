"""World Dance Registry "Pro Score" results (scores.worlddanceregistry.com).

Observed 2026-10-06 (The Chicago Classic 2026):
  event rounds list:  https://scores.worlddanceregistry.com/<event uuid>/rounds
                      title "<Event Name Year> - Scores"; table "Round Name | Results" with links
                      .../<event uuid>/rounds/<round uuid>
  round names:        "Jack & Jill - Novice - Prelim Round 1", "Jack & Jill - Novice - Final",
                      "Country Swing - Jack & Jill - Advanced - Final",
                      "Pro Am Strictly Swing - Novice - Leaders - Final"
  prelim page:        heading = round name; one table per role:
                      Callback | # | Bib # | Leaders|Followers | <judge first names>... | Scores Sum
                      callback: Y (advanced), A1/A2 (alternate), S<n> (not called back);
                      marks are points: 10 / 4.5 / 4.3 / 4.2 / 0; withdrawn names show as "***"
  final page:         Bib # | Leader | Follower | Final | <judge first names>... | 1--1 | 1--2 ...
                      ("1--k" columns are relative-placement tallies and are ignored)
There is no public list of events (ids are random UUIDs), so events are found via links on event
websites or added by URL. Pages show the year (in the title) but not exact dates.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .eepro import describe_title

PROVIDER = "wdr"
PARSER_VERSION = 1
WANTS_CONTEXT = True  # parse_sheet uses the round title and sibling round titles
BASE = "https://scores.worlddanceregistry.com"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_EVENT_RE = re.compile(rf"scores\.worlddanceregistry\.com/({_UUID})", re.I)
_ROUND_RE = re.compile(rf"/({_UUID})/rounds/({_UUID})", re.I)
_RP_COL = re.compile(r"^\d+\s*-+\s*\d+$")
_POINTS_TO_MARK = {10.0: "Y", 4.5: "A1", 4.3: "A2", 4.2: "A3", 0.0: "N"}
_TYPES = {"jack & jill", "jack and jill", "strictly swing", "pro am strictly swing", "pro am jack & jill",
          "pro am routine", "routine", "pro am", "strictly"}
_ROLES = {"leaders": "leader", "leader": "leader", "followers": "follower", "follower": "follower"}


def event_id_from(text: str | None) -> str | None:
    """Event UUID from a scores.worlddanceregistry.com URL, or a bare UUID."""
    if not text:
        return None
    m = _EVENT_RE.search(text)
    if m:
        return m.group(1).lower()
    t = text.strip()
    return t.lower() if re.fullmatch(_UUID, t, re.I) else None


def rounds_url(event_id: str) -> str:
    return f"{BASE}/{event_id}/rounds"


def parse_rounds_index(html: str, event_id: str) -> dict:
    soup = BeautifulSoup(html or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    name = re.sub(r"\s*-\s*Scores\s*$", "", title, flags=re.I).strip() or None
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urljoin(rounds_url(event_id) + "/", a["href"])
        m = _ROUND_RE.search(url)
        if not m or m.group(1).lower() != event_id.lower() or url in seen:
            continue
        seen.add(url)
        row = a.find_parent("tr")
        cells = row.find_all(["td", "th"]) if row else []
        label = cells[0].get_text(" ", strip=True) if cells else a.get_text(" ", strip=True)
        links.append((label, url))
    ym = re.search(r"(?<!\d)(19|20)\d{2}(?!\d)", name or "")
    return {"name": name, "year": int(ym.group(0)) if ym else None, "links": links}


def _round_key(title: str) -> tuple[str, int | None, str]:
    """('Jack & Jill - Novice', 2, 'prelim') for 'Jack & Jill - Novice - Prelim Round 2'."""
    parts = [p.strip() for p in title.split(" - ")]
    last = parts[-1] if parts else ""
    base = " - ".join(parts[:-1])
    if re.search(r"\bfinal\b", last, re.I) and not re.search(r"semi|quarter", last, re.I):
        return base, None, "final"
    if re.search(r"semi", last, re.I):
        return base, None, "semi"
    if re.search(r"quarter", last, re.I):
        return base, None, "quarter"
    n = re.search(r"round\s*(\d+)", last, re.I)
    return base, int(n.group(1)) if n else 1, "prelim"


def describe_round(title: str, link_titles: list[str] | None = None) -> dict:
    parts = [p.strip() for p in (title or "").split(" - ") if p.strip()]
    base, num, kind = _round_key(title or "")
    if kind == "final":
        rnd = "finals"
    elif kind in ("semi", "quarter"):
        rnd = kind + "s"
    else:
        # WDR numbers prelim rounds; map the last two to semis/quarters like other providers.
        total = max([n for t in (link_titles or []) for b, n, k in [_round_key(t)]
                     if k == "prelim" and b == base and n] or [num or 1])
        rnd = ("semis" if total >= 2 and num == total else
               "quarters" if total >= 3 and num == total - 1 else "prelims")
    body = parts[:-1]
    role = next((_ROLES[p.lower()] for p in body if p.lower() in _ROLES), None)
    body = [p for p in body if p.lower() not in _ROLES]
    style = body[0] if len(body) >= 3 and body[0].lower() not in _TYPES else None
    if style:
        body = body[1:]
    div_text = " ".join(p for p in body if p.lower() not in _TYPES)
    division = describe_title(div_text)["division"] or div_text or None
    if style and division:
        division = f"{style} {division}"
    return {"round": rnd, "role": role, "division": division, "section": " - ".join(parts[:-1]) or title}


def _float(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _tables(soup):
    for t in soup.find_all("table"):
        rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])] for tr in t.find_all("tr")]
        rows = [r for r in rows if any(r)]
        if rows:
            yield rows[0], rows[1:]


def parse_sheet(html: str, title: str | None = None, link_titles: list[str] | None = None) -> dict:
    soup = BeautifulSoup(html or "", "lxml")
    head = soup.find(["h3", "h2"])
    round_title = head.get_text(" ", strip=True) if head else (title or "")
    info = describe_round(round_title, link_titles)
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    event_title = re.sub(r"\s*-\s*Scores\s*$", "", page_title, flags=re.I).strip() or None
    sections, errors = [], []
    for header, rows in _tables(soup):
        low = [h.lower() for h in header]
        if "callback" in low:
            sections.append(_prelim(header, low, rows, round_title, info))
        elif "leader" in low and "follower" in low and "final" in low:
            sections.append(_final(header, low, rows, round_title, info))
        else:
            errors.append(f"unrecognized table in {round_title!r}: {header[:6]}")
    return {"event_title": event_title, "sections": [s for s in sections if s["entries"]], "errors": errors}


def _prelim(header, low, rows, round_title, info) -> dict:
    role_idx = next((i for i, h in enumerate(low) if h in _ROLES), 3)
    role = _ROLES.get(low[role_idx], info["role"])
    sum_idx = next((i for i, h in enumerate(low) if "sum" in h), len(header) - 1)
    judge_idx = list(range(role_idx + 1, sum_idx))
    judges = [header[i] for i in judge_idx]
    rank_idx = low.index("#") if "#" in low else None
    bib_idx = next((i for i, h in enumerate(low) if h.startswith("bib")), None)
    entries = []
    for r in rows:
        if len(r) <= sum_idx:
            continue
        name = r[role_idx].strip()
        if not name or set(name) <= {"*"}:
            continue  # withdrawn / hidden
        marks = {}
        for i, j in zip(judge_idx, judges):
            pts = _float(r[i])
            marks[j] = _POINTS_TO_MARK.get(round(pts, 1), r[i]) if pts is not None else (r[i] or None)
        cb = (r[0] or "").strip().upper()
        entries.append({"role": role, "name": name, "partner": None,
                        "bib": r[bib_idx] if bib_idx is not None else None,
                        "rank": int(r[rank_idx]) if rank_idx is not None and r[rank_idx].isdigit() else None,
                        "place": None, "marks": marks, "counts": None, "score": _float(r[sum_idx]),
                        "advanced": cb == "Y", "alternate": cb if re.fullmatch(r"A\d+", cb) else None,
                        "state": cb or None})
    return {"title": round_title + (f" ({role}s)" if role else ""), "role": role, "round": info["round"],
            "division": info["division"], "competed": len(rows), "judges": judges, "entries": entries}


def _final(header, low, rows, round_title, info) -> dict:
    li, fi, pi = low.index("leader"), low.index("follower"), low.index("final")
    end = next((i for i in range(pi + 1, len(header)) if _RP_COL.match(header[i])), len(header))
    judge_idx = list(range(pi + 1, end))
    judges = [header[i] for i in judge_idx]
    bib_idx = next((i for i, h in enumerate(low) if h.startswith("bib")), None)
    entries = []
    for r in rows:
        if len(r) <= max(li, fi, pi):
            continue
        place = int(r[pi]) if r[pi].isdigit() else None
        marks = {j: (int(r[i]) if r[i].isdigit() else r[i]) for i, j in zip(judge_idx, judges) if i < len(r) and r[i]}
        base = {"rank": place, "place": place, "marks": marks, "counts": None, "score": None,
                "advanced": None, "alternate": None, "state": None}
        lead, follow = r[li].strip(), r[fi].strip()
        bib = r[bib_idx] if bib_idx is not None else None
        if lead and set(lead) - {"*"}:
            entries.append({**base, "role": "leader", "name": lead, "partner": follow or None, "bib": bib})
        if follow and set(follow) - {"*"}:
            entries.append({**base, "role": "follower", "name": follow, "partner": lead or None, "bib": None})
    return {"title": round_title, "role": None, "round": "finals", "division": info["division"],
            "competed": len(rows), "judges": judges, "entries": entries}
