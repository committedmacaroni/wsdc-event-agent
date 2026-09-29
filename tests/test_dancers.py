import json
import unittest
from datetime import datetime, timezone

from wsdc_catalog.dancers import dancer_results
from wsdc_catalog.registry import RegistryError
from wsdc_catalog.scoresheets import index_eepro_year, index_status
from wsdc_catalog.sync import run_wsdc_sync
from tests.helpers import DAY1, day1_fetch, fx, mem
from tests.test_scoresheets import INDEX, NOW, fetcher

with open(fx("registry_29012.json"), encoding="utf-8") as f:
    TONI = json.load(f)
ROSE = {  # registry record: one result at an indexed event, one at an event with no sheets
    "follower": {"dancer": {"wscid": 4242, "first_name": "Rose", "last_name": "Landay"}, "placements": {
        "West Coast Swing": {"NOV": {"division": {"name": "Novice", "abbreviation": "NOV"}, "competitions": [
            {"role": "follower", "points": 1, "result": "F",
             "event": {"id": 77, "name": "SwingTime", "location": "Denver, CO, USA", "date": "September 2026"}},
            {"role": "follower", "points": 3, "result": "4",
             "event": {"id": 48, "name": "South Bay Dance Fling", "location": "San Jose, California, USA",
                       "date": "March 2026"}}]}}}},
    "leader": {"placements": []}, "dancer_first": "Rose", "dancer_last": "Landay", "dancer_wsdcid": 4242,
    "short_dominate_role": "Follower", "dominate_allowed": "NOV"}
NAMES = {"names": [{"first_name": "Rose", "last_name": "Landay", "wscid": 4242},
                   {"first_name": "Rosemary", "last_name": "Landay", "wscid": 9}]}


def registry(responses):
    def f(q):
        if q not in responses:
            return []
        if isinstance(responses[q], Exception):
            raise responses[q]
        return responses[q]
    return f


class TestIndexing(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        run_wsdc_sync(self.conn, day1_fetch(), now=DAY1)
        self.fetch = fetcher()
        self.run = index_eepro_year(self.conn, 2026, fetch=self.fetch, now=NOW, delay=0)

    def test_index_downloads_all_sheets(self):
        r = self.run
        self.assertEqual(r["status"], "success")
        self.assertEqual((r["events"], r["sheets_indexed"], r["sheets_failed"], r["sheets_skipped_pdf"]), (3, 3, 1, 1))
        self.assertGreater(r["entries"], 15)

    def test_rerun_skips_indexed_sheets(self):
        later = datetime(2026, 12, 1, tzinfo=timezone.utc)
        n = len(self.fetch.calls)
        r = index_eepro_year(self.conn, 2026, fetch=self.fetch, now=later, delay=0)
        self.assertEqual((r["sheets_indexed"], r["sheets_already_indexed"]), (0, 3))
        self.assertEqual(self.fetch.calls[n:], [INDEX, "https://eepro.com/results/flowfest2026/jjprelims.html"])

    def test_status(self):
        st = index_status(self.conn)
        self.assertEqual(st["providers"], ["eepro"])
        self.assertEqual(st["years"][0]["year"], "2026")


class TestDancerResults(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        run_wsdc_sync(self.conn, day1_fetch(), now=DAY1)
        index_eepro_year(self.conn, 2026, fetch=fetcher(), now=NOW, delay=0)

    def test_name_only_finds_every_event(self):
        out = dancer_results(self.conn, "Emily Madden", include_registry=False)
        self.assertEqual(out["total_events"], 1)
        ev = out["events"][0]
        self.assertEqual((ev["event_name"], ev["sources"], ev["city"]), ("SwingTime", ["scoresheets"], None))
        nov = ev["divisions"][0]
        self.assertEqual((nov["division"], nov["final_place"]), ("Novice", 1))

    def test_registry_merged_when_name_unique(self):
        out = dancer_results(self.conn, "Rose Landay",
                             registry_fetch=registry({"Rose Landay": NAMES, "4242": ROSE}))
        self.assertEqual(out["registry"]["status"], "found")
        self.assertEqual(out["registry"]["wsdc_id"], 4242)
        by_name = {e["event_name"]: e for e in out["events"]}
        st = by_name["SwingTime"]
        self.assertEqual(sorted(st["sources"]), ["scoresheets", "wsdc_registry"])
        self.assertEqual(st["registry_results"][0]["points"], 1)
        self.assertTrue(st["divisions"])
        sbdf = by_name["South Bay Dance Fling"]  # registry-only event: no sheets indexed
        self.assertEqual((sbdf["sources"], sbdf["divisions"]), (["wsdc_registry"], []))
        self.assertEqual(out["events"][0]["event_name"], "SwingTime")  # newest first

    def test_multiple_registry_matches(self):
        names = {"names": [{"first_name": "Rose", "last_name": "Landay", "wscid": 1},
                           {"first_name": "Rose", "last_name": "Landay", "wscid": 2}]}
        out = dancer_results(self.conn, "Rose Landay", registry_fetch=registry({"Rose Landay": names}))
        self.assertEqual(out["registry"]["status"], "multiple")
        self.assertEqual(out["events"][0]["sources"], ["scoresheets"])  # sheets still returned
        out = dancer_results(self.conn, "Rose Landay", wsdc_id="4242", registry_fetch=registry({"4242": ROSE}))
        self.assertEqual(out["registry"]["wsdc_id"], 4242)

    def test_registry_down_still_returns_sheets(self):
        out = dancer_results(self.conn, "Rose Landay",
                             registry_fetch=registry({"Rose Landay": RegistryError("timed out")}))
        self.assertEqual(out["registry"]["status"], "unavailable")
        self.assertEqual(out["total_events"], 1)

    def test_registry_only_dancer(self):
        out = dancer_results(self.conn, "Toni Watt", registry_fetch=registry({"Toni Watt": TONI, "29012": TONI}))
        self.assertEqual(out["total_events"], 1)
        self.assertEqual(out["events"][0]["event_name"], "South Bay Dance Fling")
        self.assertEqual(out["events"][0]["registry_results"][0]["result"], "F")

    def test_unknown_name_and_validation(self):
        out = dancer_results(self.conn, "Nobody Atall", registry_fetch=registry({}))
        self.assertEqual((out["total_events"], out["registry"]["status"]), (0, "not_found"))
        with self.assertRaises(ValueError):
            dancer_results(self.conn, "ab")
