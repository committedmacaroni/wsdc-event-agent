"""Deterministic normalization: names, aliases, dates, locations, countries."""
from __future__ import annotations

import re
import unicodedata
from datetime import date

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
HIATUS_RE = re.compile(r"\(\s*hiatus\b[^)]*\)|\bon\s+hiatus\b|\bhiatus\b", re.IGNORECASE)
_AKA_RE = re.compile(r"^(?P<base>.+?)\s+(?:aka|a\.k\.a\.?)\s+(?P<alias>.+)$", re.IGNORECASE)
_PAREN_RE = re.compile(r"^(?P<base>.+?)\s*\((?P<inner>[^()]+)\)\s*$")
_QUOTED_RE = re.compile(r'^(?P<base>.+?)\s*["\u201c\u201d](?P<inner>[^"\u201c\u201d]+)["\u201c\u201d]\s*$')

SHORT_ALIAS_MAX_LEN = 4

# Country names seen in WSDC free-text locations -> ISO 3166 alpha-3.
# Deliberately excludes ambiguous names (e.g. "Georgia", which is also a US state).
COUNTRY_NAMES = {
    "usa": "USA", "us": "USA", "u s a": "USA", "united states": "USA",
    "united states of america": "USA", "canada": "CAN", "uk": "GBR",
    "united kingdom": "GBR", "great britain": "GBR", "sweden": "SWE", "sverige": "SWE",
    "germany": "DEU", "deutschland": "DEU", "france": "FRA", "belgium": "BEL",
    "belgique": "BEL", "poland": "POL", "polska": "POL", "russia": "RUS",
    "australia": "AUS", "new zealand": "NZL", "norway": "NOR", "norge": "NOR",
    "latvia": "LVA", "italy": "ITA", "italia": "ITA", "hungary": "HUN",
    "singapore": "SGP", "switzerland": "CHE", "austria": "AUT", "netherlands": "NLD",
    "nederland": "NLD", "the netherlands": "NLD", "spain": "ESP", "espana": "ESP",
    "ireland": "IRL", "finland": "FIN", "czech republic": "CZE", "czechia": "CZE",
    "portugal": "PRT", "bulgaria": "BGR", "brazil": "BRA", "brasil": "BRA",
    "south korea": "KOR", "korea": "KOR", "slovenia": "SVN", "japan": "JPN",
    "mexico": "MEX", "denmark": "DNK", "israel": "ISR", "estonia": "EST",
    "lithuania": "LTU", "ukraine": "UKR", "romania": "ROU", "greece": "GRC",
    "china": "CHN", "taiwan": "TWN", "thailand": "THA", "croatia": "HRV",
    "serbia": "SRB", "slovakia": "SVK", "luxembourg": "LUX", "iceland": "ISL",
    "argentina": "ARG", "chile": "CHL", "colombia": "COL", "south africa": "ZAF",
    "philippines": "PHL", "hong kong": "HKG", "malaysia": "MYS", "indonesia": "IDN",
}
_EMPTY_TOKENS = {"", "n/a", "na", "-", "--", "none", "tbd"}


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize_text(s: str | None) -> str:
    s = strip_accents(s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" \t-\u2013\u2014|:,")


def detect_hiatus(raw_name: str) -> bool:
    return bool(HIATUS_RE.search(raw_name or ""))


def normalize_event_name(raw_name: str) -> str:
    """Name used for matching: no hiatus notes, no years."""
    return normalize_text(YEAR_RE.sub(" ", HIATUS_RE.sub(" ", raw_name or "")))


def split_event_name(raw_name: str) -> tuple[str, list[str]]:
    """Derive (canonical_name, aliases) from a listed name.

    'The After Party aka TAP' -> ('The After Party', ['The After Party aka TAP', 'TAP'])
    """
    full = _clean(YEAR_RE.sub(" ", HIATUS_RE.sub(" ", raw_name or "")))
    s, extracted = full, []
    for _ in range(3):
        m = _AKA_RE.match(s)
        if m:
            extracted.append(_clean(m["alias"])); s = _clean(m["base"]); continue
        m = _PAREN_RE.match(s)
        if m and normalize_text(m["inner"]):
            extracted.append(_clean(m["inner"])); s = _clean(m["base"]); continue
        m = _QUOTED_RE.match(s)
        if m and normalize_text(m["inner"]):
            extracted.append(_clean(m["inner"])); s = _clean(m["base"]); continue
        break
    canonical = s or full or (raw_name or "").strip()
    aliases, seen = [], {normalize_text(canonical)}
    for a in [full, *extracted]:
        k = normalize_text(a)
        if k and k not in seen:
            seen.add(k); aliases.append(a)
    return canonical, aliases


def is_short_alias(normalized_alias: str) -> bool:
    return len(normalized_alias.replace(" ", "")) <= SHORT_ALIAS_MAX_LEN


# ---------------------------------------------------------------- dates
_MON = r"([A-Za-z]{3,9})\.?"
_DASH = r"\s*[-\u2013\u2014]\s*"
_RANGE_FULL = re.compile(rf"^{_MON}\s+(\d{{1,2}}),?\s+(\d{{4}}){_DASH}{_MON}\s+(\d{{1,2}}),?\s+(\d{{4}})$")
_RANGE_MONTHS = re.compile(rf"^{_MON}\s+(\d{{1,2}}){_DASH}{_MON}\s+(\d{{1,2}}),?\s+(\d{{4}})$")
_RANGE_DAYS = re.compile(rf"^{_MON}\s+(\d{{1,2}}){_DASH}(\d{{1,2}}),?\s+(\d{{4}})$")
_SINGLE = re.compile(rf"^{_MON}\s+(\d{{1,2}}),?\s+(\d{{4}})$")


def _month(tok: str) -> int:
    k = tok[:3].lower()
    if k not in _MONTHS:
        raise ValueError(f"unknown month {tok!r}")
    return _MONTHS[k]


def parse_date_range(raw: str) -> tuple[date, date]:
    s = re.sub(r"\s+", " ", (raw or "").strip())
    if m := _RANGE_FULL.match(s):
        m1, d1, y1, m2, d2, y2 = m.groups()
        start, end = date(int(y1), _month(m1), int(d1)), date(int(y2), _month(m2), int(d2))
    elif m := _RANGE_MONTHS.match(s):
        m1, d1, m2, d2, y = m.groups()
        mo1, mo2 = _month(m1), _month(m2)
        y1 = int(y) - 1 if mo1 > mo2 else int(y)  # "Dec 30 - Jan 3, 2027"
        start, end = date(y1, mo1, int(d1)), date(int(y), mo2, int(d2))
    elif m := _RANGE_DAYS.match(s):
        m1, d1, d2, y = m.groups()
        start, end = date(int(y), _month(m1), int(d1)), date(int(y), _month(m1), int(d2))
    elif m := _SINGLE.match(s):
        m1, d1, y = m.groups()
        start = end = date(int(y), _month(m1), int(d1))
    else:
        raise ValueError(f"unrecognized date format {raw!r}")
    if end < start:
        raise ValueError(f"end before start in {raw!r}")
    return start, end


# ---------------------------------------------------------------- countries / locations
def country_code_from_name(text: str | None) -> str | None:
    if not text:
        return None
    t = normalize_text(text)
    if t in COUNTRY_NAMES:
        return COUNTRY_NAMES[t]
    if re.fullmatch(r"[A-Za-z]{3}", text.strip()) and text.strip().upper() in set(COUNTRY_NAMES.values()):
        return text.strip().upper()
    return None


def normalize_country_filter(value: str | None) -> str | None:
    """API filter input ('USA', 'US', 'united states', 'can') -> alpha-3."""
    if not value:
        return None
    return country_code_from_name(value) or value.strip().upper()


def parse_location(raw: str | None) -> dict:
    tokens, seen = [], set()
    for t in (raw or "").split(","):
        t = re.sub(r"\s+", " ", t).strip()
        if t.lower() in _EMPTY_TOKENS or t.lower() in seen:
            continue
        seen.add(t.lower()); tokens.append(t)
    if not tokens:
        return {"city": None, "region": None, "country_text": None, "country_code": None}
    code = text = None
    rest: list[str] = []
    for t in reversed(tokens[1:]):
        c = country_code_from_name(t)
        if c and (code is None or c == code):
            if code is None:
                code, text = c, t
            continue
        if code is None and " " in t:  # e.g. "GA USA"
            head, tail = t.rsplit(" ", 1)
            if (c2 := country_code_from_name(tail)):
                code, text, t = c2, tail, head
        rest.insert(0, t)
    return {"city": tokens[0], "region": ", ".join(rest) or None,
            "country_text": text, "country_code": code}
