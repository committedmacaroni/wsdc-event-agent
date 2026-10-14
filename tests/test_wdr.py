import unittest
from datetime import datetime, timezone

from wsdc_catalog import wdr
from wsdc_catalog.dancers import dancer_results
from wsdc_catalog.dedupe import merge_duplicates
from wsdc_catalog.enrichment import import_external_event
from wsdc_catalog.scoresheets import index_provider_events
from wsdc_catalog.websites import add_wdr_event, discover_from_websites
from tests.helpers import fx, mem

E = "82823d87-cacf-11f0-9a29-0aa72bbce9ea"
B = f"https://scores.worlddanceregistry.com/{E}"
NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def read(n):
    with open(fx(n), encoding="utf-8") as f:
        return f.read()


PAGES = {f"{B}/rounds": read("wdr_rounds.html"),
         f"{B}/rounds/ccdfc42a-0e2f-4936-9cc3-277a90b0fc7c": read("wdr_prelim.html"),
         f"{B}/rounds/fe884884-2cab-4b15-9fc4-edfeff55c4c4": read("wdr_final.html"),
         "https://chicagoclassic.example/": '<a href="https://scores.worlddanceregistry.com/' + E + '/rounds">Scores</a>'}


def fetch(url):
    if url not in PAGES:
        raise RuntimeError(f"HTTP Error 404: {url}")
    return PAGES[url]


class TestParser(unittest.TestCase):
    def test_event_id(self):
        self.assertEqual(wdr.event_id_from(f"{B}/rounds/fe884884-2cab-4b15-9fc4-edfeff55c4c4"), E)
        self.assertEqual(wdr.event_id_from(E.upper()), E)
        self.assertIsNone(wdr.event_id_from("https://example.com/"))

    def test_rounds_index(self):
        info = wdr.parse_rounds_index(read("wdr_rounds.html"), E)
        self.assertEqual((info["name"], info["year"], len(info["links"])), ("The Chicago Classic 2026", 2026, 6))
        self.assertEqual(info["links"][0][0], "Jack & Jill - Novice - Final")

    def test_round_naming(self):
        titles = [t for t, _ in wdr.parse_rounds_index(read("wdr_rounds.html"), E)["links"]]
        d = lambda t: wdr.describe_round(t, titles)
        self.assertEqual(d("Jack & Jill - Novice - Prelim Round 1")["round"], "prelims")
        self.assertEqual(d("Jack & Jill - Novice - Prelim Round 2")["round"], "quarters")
        self.assertEqual(d("Jack & Jill - Novice - Prelim Round 3")["round"], "semis")
        self.assertEqual(d("Jack & Jill - Novice - Final")["round"], "finals")
        self.assertEqual(d("Country Swing - Jack & Jill - Novice - Final")["division"], "Country Swing Novice")
        pa = d("Pro Am Strictly Swing - Novice - Leaders - Final")
        self.assertEqual((pa["division"], pa["role"]), ("Novice", "leader"))

    def test_prelim(self):
        r = wdr.parse_sheet(read("wdr_prelim.html"), link_titles=[t for t, _ in wdr.parse_rounds_index(
            read("wdr_rounds.html"), E)["links"]])
        self.assertEqual(r["errors"], [])
        lead, follow = r["sections"]
        self.assertEqual((lead["role"], lead["division"], lead["round"], lead["judges"]),
                         ("leader", "Novice", "prelims", ["Nino", "Skylar", "Matt", "Chantelle", "Kyle"]))
        self.assertEqual(len(lead["entries"]), 6)  # "***" skipped
        pk = lead["entries"][1]
        self.assertEqual((pk["name"], pk["bib"], pk["rank"], pk["marks"]["Skylar"], pk["score"], pk["advanced"]),
                         ("Pawel Kaska", "932", 15, "A1", 44.5, True))
        self.assertEqual(lead["entries"][2]["marks"]["Skylar"], "A3")
        alt = lead["entries"][3]
        self.assertEqual((alt["advanced"], alt["alternate"]), (False, "A1"))
        self.assertEqual((lead["entries"][5]["advanced"], lead["entries"][5]["alternate"]), (False, None))
        self.assertEqual(follow["entries"][1]["marks"]["Conor"], "A2")

    def test_final(self):
        r = wdr.parse_sheet(read("wdr_final.html"))
        sec = r["sections"][0]
        self.assertEqual((sec["round"], sec["division"], len(sec["judges"])), ("finals", "Novice", 7))
        lead, follow = sec["entries"][0], sec["entries"][1]
        self.assertEqual((lead["name"], lead["bib"], lead["place"], lead["partner"]),
                         ("Gabriel Diaz", "677", 1, "Katie Beechler"))
        self.assertEqual((follow["name"], follow["place"], follow["marks"]["Dawn"]), ("Katie Beechler", 1, 9))
        self.assertNotIn("1--1", lead["marks"])


class TestRegistration(unittest.TestCase):
    def test_add_by_link_and_search(self):
        conn = mem()
        reg = add_wdr_event(conn, f"{B}/rounds", fetch=fetch, now=NOW)
        self.assertEqual((reg["status"], reg["name"], reg["year"], reg["rounds"]),
                         ("registered", "The Chicago Classic 2026", 2026, 6))
        ev = conn.execute("SELECT date_precision, start_date FROM events WHERE id=?", (reg["event_id"],)).fetchone()
        self.assertEqual(tuple(ev), ("year", "2026-01-01"))
        idx = index_provider_events(conn, provider="wdr", fetch=fetch, now=NOW, delay=0)
        self.assertEqual(idx["sheets_indexed"], 2)  # other rounds 404 in this fixture set
        out = dancer_results(conn, "Katie Beechler", include_registry=False)
        div = out["events"][0]["divisions"][0]
        self.assertEqual([r["round"] for r in div["rounds"]], ["prelims", "finals"])
        self.assertEqual((div["final_place"], div["rounds"][0]["callback_pct"]), (1, 100.0))

    def test_links_to_existing_dated_event(self):
        conn = mem()
        cc = import_external_event(conn, {"source": "manual", "name": "The Chicago Classic",
                                          "start_date": "2026-11-20", "end_date": "2026-11-23"}, now=NOW)
        reg = add_wdr_event(conn, E, fetch=fetch, now=NOW)
        self.assertEqual(reg["event_id"], cc["event_id"])  # same series, same year -> same event
        ev = conn.execute("SELECT date_precision FROM events WHERE id=?", (cc["event_id"],)).fetchone()
        self.assertEqual(ev[0], "day")

    def test_registry_month_event_merges_with_year_event(self):
        conn = mem()
        reg = add_wdr_event(conn, E, fetch=fetch, now=NOW)
        import_external_event(conn, {"source": "wsdc_registry", "name": "Chicago Classic",
                                     "start_date": "2026-11-01", "date_precision": "month"}, now=NOW)
        merge_duplicates(conn)
        live = conn.execute("SELECT date_precision FROM events WHERE merged_into IS NULL").fetchall()
        self.assertEqual([r[0] for r in live], ["month"])  # month beats year

    def test_found_via_event_website(self):
        conn = mem()
        ev = import_external_event(conn, {"source": "manual", "name": "The Chicago Classic",
                                          "start_date": "2026-01-15", "end_date": "2026-01-18"}, now=NOW)
        conn.execute("UPDATE events SET website_url='https://chicagoclassic.example/' WHERE id=?", (ev["event_id"],))
        rep = discover_from_websites(conn, fetch=fetch, now=NOW, event_ids=[ev["event_id"]], delay=0)
        found = rep["events"][0]["found"][0]
        self.assertEqual((found["provider"], found["event_id"]), ("wdr", ev["event_id"]))
        self.assertEqual(rep["indexing_wdr"]["sheets_indexed"], 2)

    def test_bad_link(self):
        with self.assertRaises(ValueError):
            add_wdr_event(mem(), "https://example.com/not-wdr", fetch=fetch, now=NOW)
