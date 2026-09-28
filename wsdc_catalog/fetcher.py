"""HTTP fetching of the WSDC event list (stdlib only)."""
from __future__ import annotations

import time
import urllib.request
from dataclasses import dataclass, field

DEFAULT_URL = "https://worldsdc.com/events/"
USER_AGENT = "WSDCEventCatalog/0.1 (WCS Results Agent; daily sync)"


@dataclass
class FetchResult:
    source_url: str
    main_html: str
    confirmed_html: str | None
    errors: list[str] = field(default_factory=list)


def fetch_html(url: str, timeout: float = 30, retries: int = 2) -> str:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except Exception as e:  # noqa: BLE001 - retried, then surfaced
            last = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"fetch failed for {url}: {last}")


def fetch_wsdc(url: str = DEFAULT_URL) -> FetchResult:
    """Fetch the full list (required) and the hide-unconfirmed list (best effort)."""
    main = fetch_html(url)
    sep = "&" if "?" in url else "?"
    try:
        confirmed, errors = fetch_html(f"{url}{sep}hideunconfirmed=1"), []
    except RuntimeError as e:
        confirmed, errors = None, [f"hide-unconfirmed fetch failed: {e}"]
    return FetchResult(url, main, confirmed, errors)


def file_fetcher(main_path: str, confirmed_path: str | None = None, source_url: str = DEFAULT_URL):
    """Fetcher that reads saved HTML files (fixtures, or a snapshot being replayed)."""
    def _fetch() -> FetchResult:
        with open(main_path, encoding="utf-8") as f:
            main = f.read()
        confirmed = None
        if confirmed_path:
            with open(confirmed_path, encoding="utf-8") as f:
                confirmed = f.read()
        return FetchResult(source_url, main, confirmed)
    return _fetch
