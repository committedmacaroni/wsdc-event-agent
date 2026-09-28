from datetime import datetime, timezone
from pathlib import Path

from wsdc_catalog.db import connect
from wsdc_catalog.fetcher import file_fetcher

FIX = Path(__file__).parent / "fixtures"
DAY1 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def fx(name: str) -> str:
    return str(FIX / name)


def day1_fetch():
    return file_fetcher(fx("wsdc_day1.html"), fx("wsdc_day1_confirmed.html"))


def day2_fetch():
    return file_fetcher(fx("wsdc_day2.html"), fx("wsdc_day2_confirmed.html"))


def mem():
    return connect(":memory:")
