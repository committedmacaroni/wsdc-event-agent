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


class TestDashboardViews(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        run_wsdc_sync(self.conn, day1_fetch(), now=DAY1)
        index_eepro_year(self.conn, 2026, fetch=fetcher(), now=NOW, delay=0)
        self.out = dancer_results(self.conn, "Rose Landay", include_registry=False)

    def test_marks_have_labels_and_points(self):
        nov = [d for d in self.out["events"][0]["divisions"] if d["division"] == "Novice"][0]
        prelim = nov["rounds"][0]
        self.assertEqual(prelim["mark_details"][0], {"judge": "Bella Viramontes", "mark": "Y", "label": "Yes",
                                                     "kind": "yes", "points": 10.0})
        self.assertEqual((prelim["callback_points"], prelim["callback_max"], prelim["callback_pct"]), (40.0, 40.0, 100.0))
        semi = nov["rounds"][1]  # N, Y, Y, N
        self.assertEqual((semi["callback_pct"], semi["yes"], semi["no"]), (50.0, 2, 2))

    def test_finals_kept_separate(self):
        allin = [d for d in self.out["events"][0]["divisions"] if d["division"] == "All-In"][0]
        final = allin["rounds"][-1]
        self.assertTrue(final["is_final"])
        self.assertNotIn("callback_pct", final)
        self.assertEqual(final["mark_details"][0]["placement"], 2)
        prog = self.out["progress"]
        self.assertEqual([r["round"] for r in prog["callback_rounds"]], ["prelims", "prelims", "semis"])
        self.assertEqual((len(prog["finals"]), prog["finals"][0]["place"]), (1, 2))

    def test_alt_points(self):
        out = dancer_results(self.conn, "Octavia Betz", include_registry=False)
        r = out["events"][0]["divisions"][0]["rounds"][0]  # Y, A1, Y, N
        self.assertEqual([m["points"] for m in r["mark_details"]], [10.0, 4.5, 10.0, 0.0])
        self.assertEqual(r["callback_pct"], 61.2)

    def test_judge_breakdown(self):
        judges = {j["judge"]: j for j in self.out["judges"]}
        bella = judges["Bella Viramontes"]  # prelim Y, semi N, all-in prelim Y, all-in final 1st? (fixture)
        self.assertEqual((bella["callback_marks"], bella["yes"], bella["no"]), (3, 2, 1))
        self.assertEqual(bella["finals_judged"], 1)
        self.assertEqual(len(bella["history"]), 4)
        self.assertEqual(self.out["judges"][0]["events_judged"], 1)

    def test_summary(self):
        s = self.out["summary"]
        self.assertEqual((s["events_competed"], s["finals_made"], s["best_final_place"], s["callback_rounds"]),
                         (1, 1, 2, 3))
        self.assertEqual(s["advancement_rate"], 66.7)  # semis alternate = not advanced


class TestPointsList(unittest.TestCase):
    def test_points_awards_and_totals(self):
        conn = mem()
        run_wsdc_sync(conn, day1_fetch(), now=DAY1)
        index_eepro_year(conn, 2026, fetch=fetcher(), now=NOW, delay=0)
        out = dancer_results(conn, "Rose Landay", registry_fetch=registry({"Rose Landay": NAMES, "4242": ROSE}))
        pts = out["wsdc_points"]
        self.assertEqual(pts["total"], 4)
        self.assertEqual([(a["event_name"], a["result_label"], a["points"]) for a in pts["awards"]],
                         [("SwingTime", "Finalist", 1), ("South Bay Dance Fling", "4th", 3)])
        self.assertEqual(pts["by_division"], [{"division": "Novice", "division_abbr": "NOV", "role": "follower",
                                               "points": 4, "results": 2}])

    def test_no_registry_record(self):
        conn = mem()
        out = dancer_results(conn, "Nobody Here", registry_fetch=registry({}))
        self.assertEqual(out["wsdc_points"], {"total": 0, "awards": [], "by_division": []})


class TestRegistryOnlyDancer(unittest.TestCase):
    """Screenshot case: all results come from the registry (finalist results, no sheets)."""

    def setUp(self):
        self.conn = mem()
        rec = {"follower": {"placements": []}, "leader": {"dancer": {"wscid": 7}, "placements": {
            "West Coast Swing": {"INT": {"division": {"name": "Intermediate", "abbreviation": "INT"}, "competitions": [
                {"role": "leader", "points": 2, "result": "F",
                 "event": {"id": 1, "name": "Swingtacular", "location": "San Francisco, CA, USA", "date": "August 2026"}},
                {"role": "leader", "points": 6, "result": "3",
                 "event": {"id": 2, "name": "SOswing", "location": "Ashland, OR, USA", "date": "May 2026"}}]}}}},
            "dancer_first": "Robert", "dancer_last": "Burtt", "dancer_wsdcid": 7, "dominate_allowed": "INT"}
        self.out = dancer_results(self.conn, "Robert Burtt", registry_fetch=registry({"Robert Burtt": rec, "7": rec}))

    def test_registry_results_count_as_finals(self):
        s = self.out["summary"]
        self.assertEqual((s["finals_made"], s["best_final_place"], s["best_final_label"]), (2, 3, "3rd"))
        fin = self.out["progress"]["finals"]
        self.assertEqual([(f["event_name"], f["result_label"], f["points"], f["date_precision"]) for f in fin],
                         [("SOswing", "3rd", 6, "month"), ("Swingtacular", "Finalist", 2, "month")])

    def test_callback_stats_unavailable_not_zero(self):
        s = self.out["summary"]
        self.assertFalse(s["callback_data_available"])
        self.assertIsNone(s["avg_callback_pct"])
        self.assertIsNone(s["advancement_rate"])
        self.assertIn("aren't available", s["callback_note"])


class TestFinalsMergeSources(unittest.TestCase):
    def test_sheet_final_and_registry_result_not_double_counted(self):
        conn = mem()
        run_wsdc_sync(conn, day1_fetch(), now=DAY1)
        index_eepro_year(conn, 2026, fetch=fetcher(), now=NOW, delay=0)
        rec = {"follower": {"placements": {"West Coast Swing": {"ALL": {
            "division": {"name": "All-In", "abbreviation": "ALL"}, "competitions": [
                {"role": "follower", "points": 3, "result": "2",
                 "event": {"id": 77, "name": "SwingTime", "location": "Denver, CO, USA", "date": "September 2026"}}]}}}},
               "leader": {"placements": []}, "dancer_first": "Rose", "dancer_last": "Landay", "dancer_wsdcid": 4242}
        out = dancer_results(conn, "Rose Landay", wsdc_id="4242", registry_fetch=registry({"4242": rec}))
        fin = out["progress"]["finals"]
        self.assertEqual(len(fin), 1)
        self.assertEqual((fin[0]["place"], fin[0]["points"], fin[0]["source"]), (2, 3, "scoresheets+wsdc_registry"))
        self.assertTrue(out["summary"]["callback_data_available"])
