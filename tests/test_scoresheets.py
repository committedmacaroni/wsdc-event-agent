import json
import unittest
from datetime import datetime, timezone

from wsdc_catalog.eepro import describe_title, parse_index, parse_sheet
from wsdc_catalog.registry import lookup_competitor
from wsdc_catalog.queries import list_events
from wsdc_catalog.scoresheets import coverage, discover_eepro_year, lookup_for_events, search_by_name
from wsdc_catalog.sync import run_wsdc_sync
from tests.helpers import DAY1, day1_fetch, fx, mem

NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
INDEX = "https://eepro.com/results/2026/"


def read(name):
    with open(fx(name), encoding="utf-8") as f:
        return f.read()


PAGES = {
    INDEX: read("eepro_index_2026.html"),
    "https://eepro.com/results/swingtime2026/jjprelims.html": read("eepro_prelims.html"),
    "https://eepro.com/results/swingtime2026/jjfinals.html": read("eepro_finals.html"),
    "https://eepro.com/results/adc2026/ctstcountryswingprelims.html": "<html><body>Country swing only</body></html>",
}


def fetcher(pages=PAGES):
    calls = []

    def _f(url):
        calls.append(url)
        if url not in pages:
            raise RuntimeError(f"HTTP 404 for {url}")
        return pages[url]
    _f.calls = calls
    return _f


class TestParsing(unittest.TestCase):
    def test_titles(self):
        self.assertEqual(describe_title("Jack & Jill Follower Novice Prelims"),
                         {"role": "follower", "round": "prelims", "division": "Novice"})
        self.assertEqual(describe_title("Jack & Jill All Star Finals")["division"], "All Star")
        self.assertEqual(describe_title("J&J Prelims/Quarters/Semis")["round"], "semis")

    def test_table_and_list_layouts_agree(self):
        a, b = parse_sheet(read("eepro_prelims.html")), parse_sheet(read("eepro_prelims_list.html"))
        strip = lambda r: [(s["title"], [(e["name"], e["bib"], e["counts"], e["advanced"], e["alternate"])
                                        for e in s["entries"]]) for s in r["sections"]]
        self.assertEqual(strip(a), strip(b))
        self.assertEqual(a["errors"], [])

    def test_live_markup_snippet(self):
        """Real HTML from eepro.com: rows must not be split by source newlines or the bib <div>."""
        r = parse_sheet(read("eepro_live_snippet.html"))
        self.assertEqual((r["event_title"], r["errors"]), ("SwingTime Denver - 2026", []))
        sec = r["sections"][0]
        self.assertEqual((sec["title"], sec["competed"], sec["round"]), ("All-In Prelims", 62, "prelims"))
        self.assertEqual(sec["judges"], ["Yvonne Antonacci", "Bella Viramontes", "Jim Tigges", "Sam Vaden",
                                         "Thomas Carter"])
        self.assertEqual(len(sec["entries"]), 6)  # 3 couples -> leader + follower each
        lead = sec["entries"][2]
        self.assertEqual((lead["name"], lead["role"], lead["partner"], lead["bib"], lead["rank"], lead["advanced"]),
                         ("Aidan Keith-Hynes", "leader", "Heather Maddigan", "21", 1, True))
        self.assertEqual(lead["marks"]["Jim Tigges"], "Y")

    def test_prelim_details(self):
        secs = {s["title"]: s for s in parse_sheet(read("eepro_prelims.html"))["sections"]}
        fol = secs["Jack & Jill Follower Novice Prelims"]
        self.assertEqual((fol["competed"], len(fol["judges"])), (66, 4))
        tanya = [e for e in fol["entries"] if e["name"] == "Tanya Davis"][0]
        self.assertEqual((tanya["rank"], tanya["advanced"], tanya["marks"]["Susan Kirklin"]), (None, False, "A3"))
        alt = [e for e in fol["entries"] if e["name"] == "Rachel Hughes"][0]
        self.assertEqual((alt["advanced"], alt["alternate"]), (False, "ALT1"))
        allin = secs["All-In Prelims"]["entries"]
        self.assertEqual({(e["name"], e["role"]) for e in allin[:2]},
                         {("Andrew Opyrchal", "leader"), ("Rose Landay", "follower")})

    def test_finals_split_bibs(self):
        sec = parse_sheet(read("eepro_finals.html"))["sections"][0]
        lead, follow = sec["entries"][0], sec["entries"][1]
        self.assertEqual((lead["name"], lead["bib"], lead["place"]), ("Josh Schroeder", "321", 1))
        self.assertEqual((follow["name"], follow["bib"], follow["partner"]), ("Emily Madden", "172", "Josh Schroeder"))

    def test_index(self):
        evs, errs = parse_index(read("eepro_index_2026.html"), INDEX)
        self.assertEqual([e.key for e in evs], ["swingtime2026", "adc2026", "flowfest2026"])
        self.assertEqual(str(evs[1].end), "2026-08-02")
        self.assertEqual(errs, [])


PRELIMS = "https://eepro.com/results/swingtime2026/jjprelims.html"
FINALS = "https://eepro.com/results/swingtime2026/jjfinals.html"


def eid_for(conn, key):
    return conn.execute("SELECT event_id FROM provider_events WHERE provider_key=?", (key,)).fetchone()[0]


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        run_wsdc_sync(self.conn, day1_fetch(), now=DAY1)
        self.fetch = fetcher()
        self.run = discover_eepro_year(self.conn, 2026, fetch=self.fetch, now=NOW)

    def test_discovery_fetches_only_the_index(self):
        self.assertEqual((self.run["status"], self.run["events"]), ("success", 3))
        self.assertEqual(self.fetch.calls, [INDEX])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM scoresheet_sheets").fetchone()[0], 0)

    def test_past_events_selectable_with_results_flag(self):
        out = list_events(self.conn, {"year": "2026", "results_available": "true"})
        names = {e["name"] for e in out["items"]}
        self.assertEqual(names, {"SwingTime", "Arizona Dance Classic", "New York Flow Festival"})
        self.assertTrue(all(e["results_available"] for e in out["items"]))
        boogie = list_events(self.conn, {"search": "boogie"})["items"][0]
        self.assertFalse(boogie["results_available"])


class TestLookup(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        run_wsdc_sync(self.conn, day1_fetch(), now=DAY1)
        self.fetch = fetcher()
        discover_eepro_year(self.conn, 2026, fetch=self.fetch, now=NOW)
        self.swingtime = eid_for(self.conn, "swingtime2026")
        self.flow = eid_for(self.conn, "flowfest2026")
        self.boogie = self.conn.execute("SELECT id FROM events WHERE name='Boogie By The Bay'").fetchone()[0]

    def test_only_selected_events_fetched(self):
        n = len(self.fetch.calls)
        out = lookup_for_events(self.conn, "Rose Landay", [self.swingtime], fetch=self.fetch, now=NOW, delay=0)
        self.assertEqual(sorted(self.fetch.calls[n:]), sorted([PRELIMS, FINALS]))  # no other events touched
        ev = out["events"][0]
        self.assertEqual(ev["status"], "found")
        divs = {(d["division"], d["role"]): d for d in ev["divisions"]}
        nov = divs[("Novice", "follower")]
        self.assertEqual([r["round"] for r in nov["rounds"]], ["prelims", "semis"])
        self.assertEqual(nov["rounds"][1]["alternate"], "ALT1")
        self.assertEqual(divs[("All-In", "follower")]["final_place"], 2)
        self.assertEqual({s["status"] for s in ev["sheets"]}, {"fetched", "skipped_pdf"})

    def test_second_lookup_uses_cache(self):
        lookup_for_events(self.conn, "Rose Landay", [self.swingtime], fetch=self.fetch, now=NOW, delay=0)
        n = len(self.fetch.calls)
        later = datetime(2026, 12, 1, tzinfo=timezone.utc)
        out = lookup_for_events(self.conn, "Emily Madden", [self.swingtime], fetch=self.fetch, now=later, delay=0)
        self.assertEqual(len(self.fetch.calls), n)  # served from cached sheets
        self.assertEqual(out["events"][0]["divisions"][0]["final_place"], 1)

    def test_statuses(self):
        out = lookup_for_events(self.conn, "Rose Landay", [self.swingtime, self.flow, self.boogie, "evt_nope"],
                                fetch=self.fetch, now=NOW, delay=0)
        st = {e["event_id"]: e["status"] for e in out["events"]}
        self.assertEqual(st[self.swingtime], "found")
        self.assertEqual(st[self.flow], "not_on_sheets")        # its page errored -> nothing found
        self.assertEqual(st[self.boogie], "no_results_source")  # not on eepro
        self.assertEqual(st["evt_nope"], "event_not_found")
        flow = [e for e in out["events"] if e["event_id"] == self.flow][0]
        self.assertTrue(flow["errors"])

    def test_limits(self):
        with self.assertRaises(ValueError):
            lookup_for_events(self.conn, "Rose Landay", [], fetch=self.fetch, now=NOW)
        with self.assertRaises(ValueError):
            lookup_for_events(self.conn, "Ro", [self.swingtime], fetch=self.fetch, now=NOW)
        with self.assertRaises(ValueError):
            lookup_for_events(self.conn, "Rose Landay", [f"e{i}" for i in range(11)], fetch=self.fetch, now=NOW)

    def test_lookup_discovers_index_on_demand(self):
        conn = mem()
        run_wsdc_sync(conn, day1_fetch(), now=DAY1)
        # Catalog knows New York Flow Festival 2027 only; add 2026 via registry-style month record then look it up
        from wsdc_catalog.enrichment import import_external_event
        ev = import_external_event(conn, {"source": "manual", "name": "SwingTime", "start_date": "2026-09-10",
                                          "end_date": "2026-09-13"}, now=NOW)
        f = fetcher()
        out = lookup_for_events(conn, "Josh Schroeder", [ev["event_id"]], fetch=f, now=NOW, delay=0)
        self.assertEqual(f.calls[0], INDEX)
        self.assertEqual(out["events"][0]["status"], "found")

    def test_registry_month_event_upgraded_by_discovery(self):
        conn = mem()
        rec = {"follower": {"dancer": {"wscid": 5}, "placements": {"West Coast Swing": {"NOV": {
            "division": {"name": "Novice", "abbreviation": "NOV"}, "competitions": [
                {"role": "follower", "points": 3, "result": "4",
                 "event": {"id": 77, "name": "SwingTime", "location": "Denver, CO, USA", "date": "September 2026"}}]}}}},
            "leader": {"placements": []}, "dancer_first": "Abigail", "dancer_last": "Sewall", "dancer_wsdcid": 5}
        eid = lookup_competitor(conn, 5, fetch=lambda q: rec, now=NOW)["placements"][0]["catalog_event_id"]
        discover_eepro_year(conn, 2026, fetch=fetcher(), now=NOW)
        ev = conn.execute("SELECT start_date, date_precision FROM events WHERE id=?", (eid,)).fetchone()
        self.assertEqual(tuple(ev), ("2026-09-10", "day"))
        out = lookup_for_events(conn, "Abigail Sewall", [eid], fetch=fetcher(), now=NOW, delay=0)
        self.assertEqual(out["events"][0]["divisions"][0]["final_place"], 4)

    def test_old_parser_cache_is_reread(self):
        lookup_for_events(self.conn, "Rose Landay", [self.swingtime], fetch=self.fetch, now=NOW, delay=0)
        self.conn.execute("UPDATE scoresheet_sheets SET parser_version=1, status='empty'")
        self.conn.execute("DELETE FROM scoresheet_entries")
        n = len(self.fetch.calls)
        later = datetime(2026, 12, 1, tzinfo=timezone.utc)
        out = lookup_for_events(self.conn, "Rose Landay", [self.swingtime], fetch=self.fetch, now=later, delay=0)
        self.assertEqual(len(self.fetch.calls) - n, 2)  # both sheets re-fetched despite the event being old
        self.assertEqual(out["events"][0]["status"], "found")

    def test_search_cached_and_coverage(self):
        lookup_for_events(self.conn, "Rose Landay", [self.swingtime], fetch=self.fetch, now=NOW, delay=0)
        self.assertEqual(len(search_by_name(self.conn, "tanya DAVIS")["events"]), 1)
        self.assertEqual(coverage(self.conn)["provider_events"][0]["n"], 3)
