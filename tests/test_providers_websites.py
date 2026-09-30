import unittest
from datetime import datetime, timezone

from wsdc_catalog import scoringdance
from wsdc_catalog.dancers import dancer_results
from wsdc_catalog.enrichment import import_external_event
from wsdc_catalog.queries import get_event
from wsdc_catalog.sync import run_wsdc_sync
from wsdc_catalog.websites import add_scoring_dance_event, discover_from_websites, scan_website
from wsdc_catalog.scoresheets import index_provider_events
from tests.helpers import DAY1, day1_fetch, fx, mem
from tests.test_scoresheets import PAGES, read

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
SITE = "https://swingresolution.co.uk/"
SD = {
    "https://scoring.dance/enUS/events/195/results/": read("sd_index_195.html"),
    "https://scoring.dance/enUS/events/195/results/3073.html": read("sd_round_3073.html"),
    "https://scoring.dance/enUS/events/195/results/3074.html": read("sd_round_3074.html"),
    SITE: read("site_swingres.html"),
    "https://swingresolution.co.uk/competitions/results": read("site_swingres_results.html"),
}


def fetcher():
    pages = {**PAGES, **SD}
    calls = []

    def f(url):
        calls.append(url)
        if url not in pages:
            raise RuntimeError(f"404 {url}")
        return pages[url]
    f.calls = calls
    return f


class TestScoringDanceParser(unittest.TestCase):
    def test_index(self):
        info = scoringdance.parse_results_index(read("sd_index_195.html"), "195")
        self.assertEqual((info["name"], str(info["date"])), ("Swing Resolution 2025", "2025-01-23"))
        self.assertEqual([u for _, u in info["links"]],
                         ["https://scoring.dance/enUS/events/195/results/3073.html",
                          "https://scoring.dance/enUS/events/195/results/3074.html"])  # locale normalized, deduped

    def test_prelim(self):
        r = scoringdance.parse_sheet(read("sd_round_3073.html"))
        self.assertEqual(r["event_title"], "Swing Resolution 2025")
        self.assertEqual(r["errors"], [])
        lead, follow = r["sections"]
        self.assertEqual((lead["role"], lead["division"], lead["round"]), ("leader", "Advanced", "prelims"))
        self.assertEqual(lead["judges"], ["Jeff Mumford", "Naomi", "Tren", "Patty Vo", "Gary Jobst"])
        henry = lead["entries"][0]
        self.assertEqual((henry["name"], henry["bib"], henry["wsdc_id"], henry["advanced"], henry["score"]),
                         ("Henry Leonard", "4", 17901, True, 40.0))
        self.assertNotIn("Gary Jobst", henry["marks"])  # head judge doesn't mark prelims
        mark = lead["entries"][2]
        self.assertEqual((mark["name"], mark["marks"]["Tren"], mark["rank"]), ("Mark Miller", "A1", 3))
        self.assertFalse(lead["entries"][3]["advanced"])
        self.assertEqual(follow["entries"][1]["marks"]["Lecie Langille"], "N")

    def test_final(self):
        r = scoringdance.parse_sheet(read("sd_round_3074.html"))
        self.assertEqual(r["errors"], [])
        sec = r["sections"][0]
        self.assertEqual((sec["division"], sec["round"], len(sec["judges"])), ("Advanced", "finals", 8))
        lead, follow = sec["entries"][0], sec["entries"][1]
        self.assertEqual((lead["name"], lead["role"], lead["wsdc_id"], lead["place"], lead["bib"], lead["partner"]),
                         ("Fran Vidal", "leader", 18215, 1, "340", "Ellen Dacombe"))
        self.assertEqual((follow["name"], follow["role"], follow["wsdc_id"], follow["place"]),
                         ("Ellen Dacombe", "follower", 9498, 1))
        self.assertEqual((lead["marks"]["Tara Trafzer"], lead["marks"]["Paul Warden"]), (6, 1))
        self.assertNotIn("Gary Jobst", lead["marks"])

    def test_unknown_layout_reported_not_guessed(self):
        r = scoringdance.parse_sheet("<h1>Strictly final results - X 2025</h1><table><tr><th>Rank</th></tr>"
                                     "<tr><td>1</td></tr></table>")
        self.assertEqual(r["sections"], [])
        self.assertIn("finals layout not yet supported", r["errors"][0])

    def test_event_number_from_any_locale(self):
        self.assertEqual(scoringdance.event_number("https://scoring.dance/frFR/events/392/results/6509.html"), "392")


class TestWebsites(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        run_wsdc_sync(self.conn, day1_fetch(), now=DAY1)
        ev = import_external_event(self.conn, {"source": "manual", "name": "Swing Resolution",
                                               "start_date": "2025-01-23", "end_date": "2025-01-26"}, now=NOW)
        self.eid = ev["event_id"]
        self.conn.execute("UPDATE events SET website_url=? WHERE id=?", (SITE, self.eid))

    def test_scan_finds_provider_and_other_links(self):
        found = scan_website(SITE, fetch=fetcher(), delay=0)
        self.assertEqual((found["scoring_dance"], found["eepro"]), ({"195"}, {"swingtime2026"}))
        self.assertEqual(found["other"][0][1], "https://swingresolution.co.uk/files/2025-routines-results.pdf")

    def test_discovery_links_indexes_and_searches(self):
        f = fetcher()
        rep = discover_from_websites(self.conn, fetch=f, now=NOW, event_ids=[self.eid], delay=0)
        sd = [x for x in rep["events"][0]["found"] if x["provider"] == "scoring_dance"][0]
        self.assertEqual((sd["status"], sd["event_id"]), ("registered", self.eid))  # dates agree -> same event
        self.assertEqual(rep["indexing"]["sheets_indexed"], 2)
        out = dancer_results(self.conn, "Mark Miller", include_registry=False)
        self.assertEqual(out["events"][0]["event_id"], self.eid)
        rnd = out["events"][0]["divisions"][0]["rounds"][0]
        self.assertEqual((rnd["round"], rnd["bib"], rnd["score"], rnd["advanced"]), ("prelims", "7", 34.5, True))
        self.assertEqual(out["events"][0]["website_results_links"][0]["text"], "Routines results (PDF)")
        self.assertTrue(get_event(self.conn, self.eid)["website_results_links"])

    def test_previous_years_results_not_attached_to_wrong_year(self):
        self.conn.execute("UPDATE events SET start_date='2026-01-22', end_date='2026-01-25', year=2026 WHERE id=?",
                          (self.eid,))
        ev = self.conn.execute("SELECT * FROM events WHERE id=?", (self.eid,)).fetchone()
        reg = add_scoring_dance_event(self.conn, "195", fetch=fetcher(), now=NOW, catalog_event=ev)
        self.assertNotEqual(reg["event_id"], self.eid)  # 2025 results -> their own 2025 occurrence
        other = self.conn.execute("SELECT start_date FROM events WHERE id=?", (reg["event_id"],)).fetchone()
        self.assertEqual(other[0], "2025-01-23")

    def test_upcoming_events_and_recent_checks_skipped(self):
        f = fetcher()
        discover_from_websites(self.conn, fetch=f, now=NOW, delay=0, index=False)
        n = len(f.calls)
        rep = discover_from_websites(self.conn, fetch=f, now=NOW, delay=0, index=False)
        self.assertEqual((rep["events_checked"], len(f.calls)), (0, n))  # checked within 7 days
        # WSDC calendar events (all in the future relative to their dates) weren't scanned
        self.assertNotIn("https://boogiebythebay.com", f.calls)

    def test_broken_website_recorded(self):
        self.conn.execute("UPDATE events SET website_url='https://down.example/' WHERE id=?", (self.eid,))
        rep = discover_from_websites(self.conn, fetch=fetcher(), now=NOW, event_ids=[self.eid], delay=0)
        self.assertTrue(rep["errors"])
        st = self.conn.execute("SELECT status FROM website_checks WHERE event_id=?", (self.eid,)).fetchone()[0]
        self.assertEqual(st, "error")

    def test_register_by_number(self):
        reg = add_scoring_dance_event(self.conn, "195", fetch=fetcher(), now=NOW)
        self.assertEqual((reg["status"], reg["rounds"]), ("registered", 2))
        idx = index_provider_events(self.conn, provider="scoring_dance", fetch=fetcher(), now=NOW, delay=0)
        self.assertEqual(idx["sheets_indexed"], 2)


class TestWsdcIdMatching(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        add_scoring_dance_event(self.conn, "195", fetch=fetcher(), now=NOW)
        index_provider_events(self.conn, provider="scoring_dance", fetch=fetcher(), now=NOW, delay=0)

    def test_wsdc_id_matches_despite_spelling(self):
        # Registry spells the name differently from the sheet; the WSDC id on the sheet still links them.
        rec = {"leader": {"dancer": {"wscid": 17901}, "placements": []}, "follower": {"placements": []},
               "dancer_first": "Henry", "dancer_last": "Leonard-Smith", "dancer_wsdcid": 17901}
        out = dancer_results(self.conn, "Henry Leonard-Smith", wsdc_id="17901", registry_fetch=lambda q: rec)
        self.assertEqual(out["total_events"], 1)
        self.assertEqual(out["events"][0]["divisions"][0]["rounds"][0]["bib"], "4")

    def test_same_name_different_dancer_excluded(self):
        rec = {"leader": {"dancer": {"wscid": 99}, "placements": []}, "follower": {"placements": []},
               "dancer_first": "Henry", "dancer_last": "Leonard", "dancer_wsdcid": 99}
        out = dancer_results(self.conn, "Henry Leonard", wsdc_id="99", registry_fetch=lambda q: rec)
        self.assertEqual(out["total_events"], 0)  # sheet says this Henry Leonard is #17901, not #99


class TestFinalsInDancerResults(unittest.TestCase):
    def test_prelim_and_final_combined(self):
        conn = mem()
        add_scoring_dance_event(conn, "195", fetch=fetcher(), now=NOW)
        index_provider_events(conn, provider="scoring_dance", fetch=fetcher(), now=NOW, delay=0)
        out = dancer_results(conn, "Ellen Dacombe", include_registry=False)
        div = out["events"][0]["divisions"][0]
        self.assertEqual((div["division"], div["role"]), ("Advanced", "follower"))
        self.assertEqual([r["round"] for r in div["rounds"]], ["prelims", "finals"])
        self.assertEqual((div["final_place"], div["rounds"][1]["partner"]), (1, "Fran Vidal"))


class TestScoringDanceScan(unittest.TestCase):
    """Event numbers 1-3 exist (2 has no results yet), 4-5 missing, 6 exists."""

    def setUp(self):
        self.conn = mem()
        idx = read("sd_index_195.html")
        self.pages = {}
        for n in (1, 3, 6):
            self.pages[f"https://scoring.dance/enUS/events/{n}/results/"] = idx.replace("/195/", f"/{n}/")
            self.pages[f"https://scoring.dance/enUS/events/{n}/results/3073.html"] = read("sd_round_3073.html")
            self.pages[f"https://scoring.dance/enUS/events/{n}/results/3074.html"] = read("sd_round_3074.html")
        self.pages["https://scoring.dance/enUS/events/2/results/"] = "<html><h1>Future Event 2027 results</h1></html>"

    def fetch(self, url):
        if url not in self.pages:
            raise RuntimeError(f"HTTP Error 404: Not Found ({url})")
        return self.pages[url]

    def test_backfill_walks_until_misses(self):
        from wsdc_catalog import websites
        old = websites.SD_MISS_LIMIT
        websites.SD_MISS_LIMIT = 3
        try:
            rep = websites.scan_scoring_dance(self.conn, start=1, fetch=self.fetch, now=NOW, delay=0)
        finally:
            websites.SD_MISS_LIMIT = old
        self.assertEqual((rep["registered"], rep["no_results"], rep["highest_found"]), (3, 1, 6))
        self.assertEqual(rep["scanned_range"], [1, 9])
        self.assertEqual(rep["errors"], [])  # 404s are expected, not errors
        self.assertGreater(rep["indexing"]["sheets_indexed"], 0)

    def test_date_falls_back_to_round_page(self):
        idx = self.pages["https://scoring.dance/enUS/events/1/results/"]
        self.pages["https://scoring.dance/enUS/events/1/results/"] = idx.replace("at 01/23/2025", "")
        reg = add_scoring_dance_event(self.conn, "1", fetch=self.fetch, now=NOW)
        ev = self.conn.execute("SELECT start_date FROM events WHERE id=?", (reg["event_id"],)).fetchone()
        self.assertEqual(ev[0], "2025-01-23")

    def test_results_reach_dancer_search(self):
        from wsdc_catalog.websites import scan_scoring_dance
        scan_scoring_dance(self.conn, start=1, end=1, fetch=self.fetch, now=NOW, delay=0)
        out = dancer_results(self.conn, "Henry Leonard", include_registry=False)
        self.assertEqual(out["total_events"], 1)
        self.assertEqual(out["events"][0]["provider"], "scoring_dance")
