import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from wsdc_catalog.db import connect, init_schema
from wsdc_catalog.registry import (RegistryError, cached_find, lookup_competitor, parse_find, parse_month,
                                   search_competitors)
from wsdc_catalog.sync import run_wsdc_sync
from tests.helpers import DAY1, day1_fetch, fx, mem

with open(fx("registry_29012.json"), encoding="utf-8") as f:
    TONI = json.load(f)

# Synthetic record with a result at an event already in the catalog (day-precision).
MULTI = {
    "leader": {"dancer": {"wscid": 777, "first_name": "Sam", "last_name": "Lee"}, "placements": []},
    "follower": {"dancer": {"wscid": 777, "first_name": "Sam", "last_name": "Lee"}, "placements": {
        "West Coast Swing": {
            "INT": {"division": {"name": "Intermediate", "abbreviation": "INT"}, "competitions": [
                {"role": "follower", "points": 6, "result": "3",
                 "event": {"id": 901, "name": "Boogie By The Bay", "location": "San Francisco, CA, USA",
                           "url": "https://boogiebythebay.com", "date": "October 2026"}}]},
            "NOV": {"division": {"name": "Novice", "abbreviation": "NOV"}, "competitions": [
                {"role": "follower", "points": 1, "result": "F",
                 "event": {"id": 48, "name": "South Bay Dance Fling", "location": "San Jose, California, USA",
                           "url": "https://sbdf.dance/", "date": "September 2025"}}]}}}},
    "dancer_first": "Sam", "dancer_last": "Lee", "dancer_wsdcid": 777,
    "short_dominate_role": "Follower", "short_non_dominate_role": "Leader",
}
NAMES = {"names": [{"first_name": "Toni", "last_name": "Watt", "wscid": 29012},
                   {"first_name": "Tony", "last_name": "Watts", "wscid": 11111}]}


def fake_fetch(responses):
    calls = []

    def _fetch(q):
        calls.append(q)
        return responses[q]
    _fetch.calls = calls
    return _fetch


class TestParse(unittest.TestCase):
    def test_real_record(self):
        p = parse_find(TONI)
        self.assertEqual((p["type"], p["wsdc_id"], p["first_name"], p["primary_role"]),
                         ("dancer", 29012, "Toni", "Follower"))
        self.assertEqual(len(p["placements"]), 1)
        pl = p["placements"][0]
        self.assertEqual((pl["role"], pl["division_abbr"], pl["result"], pl["points"]), ("follower", "NOV", "F", 1))
        self.assertEqual(pl["registry_event"]["month"], "2026-09")

    def test_candidates_and_none(self):
        self.assertEqual(len(parse_find(NAMES)["candidates"]), 2)
        self.assertEqual(parse_find([])["type"], "none")
        with self.assertRaises(RegistryError):
            parse_find({"something": "else"})

    def test_parse_month(self):
        self.assertEqual(parse_month("September 2026"), (2026, 9))
        self.assertEqual(parse_month("Sept 2026"), (2026, 9))
        self.assertIsNone(parse_month("2026"))


class TestLookup(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        run_wsdc_sync(self.conn, day1_fetch(), now=DAY1)

    def test_historical_event_created_and_reused(self):
        fetch = fake_fetch({"29012": TONI})
        out = lookup_competitor(self.conn, 29012, fetch=fetch)
        pl = out["placements"][0]
        self.assertEqual(pl["catalog_link"], "created")
        ev = self.conn.execute("SELECT * FROM events WHERE id=?", (pl["catalog_event_id"],)).fetchone()
        self.assertEqual((ev["source"], ev["lifecycle_status"], ev["date_precision"], ev["start_date"], ev["country"]),
                         ("wsdc_registry", "historical", "month", "2026-09-01", "USA"))
        # Another dancer's result at the same registry event links to the same series.
        out2 = lookup_competitor(self.conn, 777, fetch=fake_fetch({"777": MULTI}))
        sbdf = [p for p in out2["placements"] if p["registry_event"]["id"] == 48][0]
        self.assertEqual(sbdf["catalog_link"], "created")  # different year -> new occurrence
        s1 = self.conn.execute("SELECT series_id FROM events WHERE id=?", (pl["catalog_event_id"],)).fetchone()[0]
        s2 = self.conn.execute("SELECT series_id FROM events WHERE id=?", (sbdf["catalog_event_id"],)).fetchone()[0]
        self.assertEqual(s1, s2)

    def test_links_to_existing_calendar_event(self):
        out = lookup_competitor(self.conn, 777, fetch=fake_fetch({"777": MULTI}))
        boogie = [p for p in out["placements"] if p["registry_event"]["id"] == 901][0]
        self.assertEqual(boogie["catalog_link"], "matched")
        self.assertEqual(boogie["catalog_event"]["start_date"], "2026-10-08")

    def test_repeat_lookup_idempotent(self):
        fetch = fake_fetch({"29012": TONI})
        a = lookup_competitor(self.conn, 29012, fetch=fetch)
        b = lookup_competitor(self.conn, 29012, fetch=fetch)
        self.assertEqual(a["placements"][0]["catalog_event_id"], b["placements"][0]["catalog_event_id"])
        self.assertEqual(b["placements"][0]["catalog_link"], "matched")
        self.assertEqual(len(fetch.calls), 1)  # second call served from cache

    def test_cache_expires(self):
        fetch = fake_fetch({"29012": TONI})
        t0 = datetime(2026, 9, 24, tzinfo=timezone.utc)
        cached_find(self.conn, "29012", fetch, now=t0)
        cached_find(self.conn, "29012", fetch, now=t0 + timedelta(hours=13))
        self.assertEqual(len(fetch.calls), 2)

    def test_search(self):
        out = search_competitors(self.conn, "Watt", fetch=fake_fetch({"Watt": NAMES}))
        self.assertEqual([i["wsdc_id"] for i in out["items"]], [29012, 11111])
        out = search_competitors(self.conn, "Toni Watt", fetch=fake_fetch({"Toni Watt": TONI}))
        self.assertEqual(out["items"], [{"wsdc_id": 29012, "first_name": "Toni", "last_name": "Watt"}])

    def test_bad_id(self):
        with self.assertRaises(ValueError):
            lookup_competitor(self.conn, "abc", fetch=fake_fetch({}))
        self.assertIsNone(lookup_competitor(self.conn, 5, fetch=fake_fetch({"5": []})))


class TestMigration(unittest.TestCase):
    def test_v1_database_upgrades(self):
        from wsdc_catalog.db import SCHEMA
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        init_schema(conn)
        from wsdc_catalog.db import SCHEMA_VERSION
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"series_external_refs", "registry_cache", "scoresheet_sheets",
                         "scoresheet_entries"} <= tables)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
        self.assertIn("date_precision", cols)
