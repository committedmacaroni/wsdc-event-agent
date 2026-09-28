"""Deterministic parser for the WSDC event list HTML table (https://worldsdc.com/events/)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from .normalize import (country_code_from_name, detect_hiatus, normalize_event_name,
                        parse_date_range, parse_location, split_event_name)

_HEADER_MAP = {
    "date": "date", "dates": "date",
    "event name": "name", "event": "name", "name": "name",
    "event location": "location", "location": "location",
    "country": "country",
}
_STATUS_RE = re.compile(r"\b(registry|trial)\s+event\b", re.IGNORECASE)
_DOUBLE_SCHEME_RE = re.compile(r"^https?://(?=https?://)", re.IGNORECASE)
_NON_COUNTRY_FLAGS = {"", "TRANSPARENT", "NONE", "BLANK"}


@dataclass
class ParsedEvent:
    row_index: int
    raw: dict
    name: str
    normalized_name: str
    canonical_name: str
    aliases: list[str]
    event_status: str
    hiatus: bool
    start_date: date
    end_date: date
    location_raw: str
    city: str | None
    region: str | None
    country: str | None
    country_raw: str | None
    website_url: str | None
    unconfirmed_hint: bool | None
    warnings: list[str] = field(default_factory=list)

    @property
    def match_key(self) -> tuple:
        """Key used to line rows up between the full and hide-unconfirmed lists."""
        return (self.normalized_name, self.start_date.isoformat(), self.country)


@dataclass
class ParseResult:
    rows: list[ParsedEvent]
    errors: list[str]
    table_found: bool


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def _clean_url(href: str | None) -> str | None:
    if not href:
        return None
    href = _DOUBLE_SCHEME_RE.sub("", href.strip())
    return href or None


def _flag_code(cell) -> str | None:
    """Country code from the '?country=XXX' link or the flag image file name."""
    if cell is None:
        return None
    for a in cell.find_all("a", href=True):
        vals = parse_qs(urlparse(a["href"]).query, keep_blank_values=True).get("country")
        if vals is not None:
            return (vals[0] or "").strip().upper()
    img = cell.find("img", src=True)
    if img:
        return PurePosixPath(urlparse(img["src"]).path).stem.upper()
    txt = _text(cell)
    return txt.upper() if txt else None


def _find_table(soup):
    for table in soup.find_all("table"):
        header_row = None
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            labels = [_text(c).lower() for c in cells]
            if "date" in labels and any(l in ("event name", "event") for l in labels):
                header_row = tr
                break
        if header_row is not None:
            cols = {}
            for i, c in enumerate(header_row.find_all(["th", "td"])):
                key = _HEADER_MAP.get(_text(c).lower())
                if key and key not in cols:
                    cols[key] = i
            return table, header_row, cols
    return None, None, None


def parse_event_list(html: str) -> ParseResult:
    soup = BeautifulSoup(html or "", "lxml")
    table, header_row, cols = _find_table(soup)
    if table is None:
        return ParseResult([], ["event table not found (no table with 'Date' and 'Event Name' headers)"], False)
    missing = {"date", "name"} - set(cols)
    if missing:
        return ParseResult([], [f"event table missing columns: {sorted(missing)}"], False)

    rows, errors = [], []
    past_header = False
    for idx, tr in enumerate(table.find_all("tr")):
        if tr is header_row:
            past_header = True
            continue
        if not past_header:
            continue
        tds = tr.find_all("td")
        if not tds:
            continue

        def cell(key):
            i = cols.get(key)
            return tds[i] if i is not None and i < len(tds) else None

        date_cell, name_cell = cell("date"), cell("name")
        loc_cell, country_cell = cell("location"), cell("country")
        link = name_cell.find("a") if name_cell else None
        name_cell_text = _text(name_cell)
        name = _text(link) if link and _text(link) else _STATUS_RE.sub("", name_cell_text).strip()
        status_m = _STATUS_RE.search(name_cell_text)
        date_raw = _text(date_cell)
        location_raw = _text(loc_cell)
        flag = _flag_code(country_cell)
        row_classes = " ".join(tr.get("class", [])).lower()
        raw = {
            "date": date_raw, "name_cell": name_cell_text, "name": name,
            "href": link.get("href") if link else None, "location": location_raw,
            "country_flag": flag, "row_class": row_classes or None,
        }
        if not name:
            errors.append(f"row {idx}: empty event name; raw={raw}")
            continue
        try:
            start, end = parse_date_range(date_raw)
        except ValueError as e:
            errors.append(f"row {idx}: {e}; event={name!r}")
            continue

        warnings = []
        loc = parse_location(location_raw)
        flag_code = flag if flag and re.fullmatch(r"[A-Z]{3}", flag) and flag not in _NON_COUNTRY_FLAGS else None
        text_code = loc["country_code"]
        if flag_code and text_code and flag_code != text_code:
            warnings.append(f"country flag {flag_code} disagrees with location text "
                            f"{loc['country_text']!r}; using {text_code}")
        country = text_code or flag_code

        hint = None
        if "unconfirmed" in row_classes or "unconfirmed" in name_cell_text.lower():
            hint = True
        canonical, aliases = split_event_name(name)
        rows.append(ParsedEvent(
            row_index=idx, raw=raw, name=name, normalized_name=normalize_event_name(name),
            canonical_name=canonical, aliases=aliases,
            event_status=status_m.group(1).lower() if status_m else "unknown",
            hiatus=detect_hiatus(name), start_date=start, end_date=end,
            location_raw=location_raw, city=loc["city"], region=loc["region"],
            country=country, country_raw=flag or None, website_url=_clean_url(raw["href"]),
            unconfirmed_hint=hint, warnings=warnings,
        ))
    return ParseResult(rows, errors, True)
