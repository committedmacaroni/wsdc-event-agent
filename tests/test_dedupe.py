import runpy
import unittest
from datetime import datetime, timezone
from pathlib import Path

from wsdc_catalog.dancers import dancer_results
from wsdc_catalog.dedupe import merge_duplicates, names_related
from wsdc_catalog.queries import get_event, list_events
from wsdc_catalog.registry import lookup_competitor
from wsdc_catalog.scoresheets import division_label, index_eepro_year
from wsdc_catalog.sync import run_wsdc_sync
from tests.helpers import DAY1, day1_fetch, mem

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
gen = runpy.run_path(str(Path(__file__).parent / "fixtures" / "make_eepro_fixtures.py"))
JUDGES = ["Ann Judge", "Bob Judge", "Cy Judge"]
SHEET = gen["table_sheet"]([
    ("Jack & Jill Leader Intermediate Prelims - 40 competed", JUDGES,
     ["14|Robert Burtt|Y|N|Y|55|2-0-1|20||"]),
    ("Jack & Jill Leader Prelims - 20 competed", JUDGES,   # no division in the title
     ["3|Robert Burtt|Y|Y|Y|55|3-0-0|30|X|"]),
], "prelim")
INDEX = ('<html><body><ul><li> August 6-9, 2026 - Swingtacular<ul>'
         '<li><a href="https://eepro.com/results/swingtacular2026/jjprelims.html">J&amp;J Prelims</a></li>'
         '</ul></li></ul></body></html>')
PAGES = {"https://eepro.com/results/2026/": INDEX,
         "https://eepro.com/results/swingtacular2026/jjprelims.html": SHEET}
REC = {"leader": {"dancer": {"wscid": 7}, "placements": {"West Coast Swing": {"INT": {
    "division": {"name": "Intermediate", "abbreviation": "INT"}, "competitions": [
        {"role": "leader", "points": 2, "result": "F",
         "event": {"id": 501, "name": "Swingtacular: The Galactic Open", "location": "San Francisco, CA, USA",
                   "date": "August 2026"}}]}}}},
       "follower": {"placements": []}, "dancer_first": "Robert", "dancer_last": "Burtt", "dancer_wsdcid": 7}


def fetch(url):
    return PAGES[url]


class TestNames(unittest.TestCase):
    def test_related(self):
        self.assertTrue(names_related("Swingtacular", "Swingtacular: The Galactic Open"))
        self.assertFalse(names_related("Boogie By The Bay", "Swingtacular"))
        self.assertFalse(names_related("Swing Fling", "Swing Resolution"))

    def test_division_label(self):
        self.assertEqual(division_label(None, "Jack & Jill Leader Prelims"), "Jack & Jill")
        self.assertEqual(division_label(None, "Sophisticated Strictly Swing Finals"), "Sophisticated Strictly Swing")
        self.assertEqual(division_label("Novice", "anything"), "Novice")


class TestOneEvent(unittest.TestCase):
    def assert_single_event(self, conn):
        out = dancer_results(conn, "Robert Burtt", wsdc_id="7", registry_fetch=lambda q: REC)
        self.assertEqual(out["total_events"], 1, [e["event_name"] for e in out["events"]])
        ev = out["events"][0]
        self.assertEqual(sorted(ev["sources"]), ["scoresheets", "wsdc_registry"])
        self.assertEqual(ev["date_precision"], "day")
        labels = sorted(d["division_label"] for d in ev["divisions"])
        self.assertEqual(labels, ["Intermediate", "Jack & Jill"])  # never empty
        return out

    def test_registry_first_then_sheets(self):
        conn = mem()
        lookup_competitor(conn, 7, fetch=lambda q: REC, now=NOW)
        index_eepro_year(conn, 2026, fetch=fetch, now=NOW, delay=0)
        self.assert_single_event(conn)

    def test_sheets_first_then_registry(self):
        conn = mem()
        index_eepro_year(conn, 2026, fetch=fetch, now=NOW, delay=0)
        self.assert_single_event(conn)

    def test_repair_existing_duplicates(self):
        """Databases built before v0.9 already hold both copies; dedupe merges them without losing ids."""
        conn = mem()
        from wsdc_catalog import dedupe
        orig = dedupe.find_related_event
        dedupe.find_related_event = lambda *a, **k: None   # simulate the old behaviour
        try:
            lookup_competitor(conn, 7, fetch=lambda q: REC, now=NOW)
            index_eepro_year(conn, 2026, fetch=fetch, now=NOW, delay=0)
        finally:
            dedupe.find_related_event = orig
        ids = [r["id"] for r in conn.execute("SELECT id FROM events")]
        rep = merge_duplicates(conn)
        live = [r["id"] for r in conn.execute("SELECT id FROM events WHERE merged_into IS NULL")]
        self.assertEqual(len(live), 1)
        self.assert_single_event(conn)
        for old in ids:  # every id ever handed out still resolves
            self.assertEqual(get_event(conn, old)["id"], live[0])
        self.assertEqual(merge_duplicates(conn)["merged"], 0)  # idempotent

    def test_same_source_never_merged(self):
        conn = mem()
        run_wsdc_sync(conn, day1_fetch(), now=DAY1)
        merge_duplicates(conn)
        wcs = list_events(conn, {"search": "wcs festival"})["items"]
        self.assertEqual(len(wcs), 2)  # two WSDC calendar listings a day apart stay separate


class TestUnlinkedSheetsStillNamed(unittest.TestCase):
    def test_event_name_from_provider(self):
        conn = mem()
        index_eepro_year(conn, 2026, fetch=fetch, now=NOW, delay=0)
        conn.execute("UPDATE scoresheet_entries SET event_id=NULL")
        out = dancer_results(conn, "Robert Burtt", include_registry=False)
        self.assertEqual((out["events"][0]["event_name"], out["events"][0]["start_date"]),
                         ("Swingtacular", "2026-08-06"))
