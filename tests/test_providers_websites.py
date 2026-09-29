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
        lead, follow = r["sections"]
        self.assertEqual((lead["role"], lead["division"], lead["round"], lead["judges"]),
                         ("leader", "Advanced", "prelims", ["JM", "N", "T", "PV", "GJ"]))
        mark = lead["entries"][1]
        self.assertEqual((mark["name"], mark["bib"], mark["score"], mark["marks"]["T"], mark["rank"]),
                         ("Mark Miller", "7", 34.5, "A1", 2))
        self.assertEqual(follow["entries"][1]["marks"]["LL"], "N")

    def test_unknown_final_layout_reported_not_guessed(self):
        r = scoringdance.parse_sheet(read("sd_round_3074.html"))
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
        self.assertEqual((rnd["round"], rnd["bib"], rnd["score"]), ("prelims", "7", 34.5))
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
