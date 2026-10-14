"""Regression: Boogie By The Bay 2026 (scoring.dance) never reached the app.

scoring.dance creates an event's number months ahead. The backfill saw it with no results and
forgot it; the daily scan never came back. The event website was checked once, before results
were posted, then not for 7 days; and a named link like scoring.dance/events/bbtb2024/results
was ignored.
"""
import unittest
from datetime import datetime, timedelta, timezone

from wsdc_catalog import scoringdance, websites
from wsdc_catalog.dancers import dancer_results
from wsdc_catalog.enrichment import import_external_event
from wsdc_catalog.websites import add_scoring_dance_event, discover_from_websites, scan_scoring_dance
from tests.helpers import mem
from tests.test_providers_websites import read

BOOGIE = 420
EMPTY = "<html><head><title>Boogie By the Bay 2026 results</title></head><body><h1>Boogie By the Bay 2026 results</h1></body></html>"
D_BACKFILL = datetime(2026, 9, 1, tzinfo=timezone.utc)
D_EVENT = datetime(2026, 10, 9, tzinfo=timezone.utc)
D_AFTER = datetime(2026, 10, 13, tzinfo=timezone.utc)


def with_results(n, date="10/08/2026", name="Boogie By the Bay 2026"):
    idx = read("sd_index_195.html").replace("/195/", f"/{n}/").replace("Swing Resolution 2025", name) \
        .replace("01/23/2025", date)
    return idx


class FakeSD:
    def __init__(self):
        self.pages = {}
        for n in (430, 431):  # newer events created later, already with results
            self.pages[self.idx(n)] = with_results(n, "11/20/2026", f"Other Event {n}")
        self.pages[self.idx(BOOGIE)] = EMPTY
        self.calls = []

    @staticmethod
    def idx(n):
        return f"https://scoring.dance/enUS/events/{n}/results/"

    def post_results(self):
        self.pages[self.idx(BOOGIE)] = with_results(BOOGIE)
        for r in ("3073", "3074"):
            self.pages[f"https://scoring.dance/enUS/events/{BOOGIE}/results/{r}.html"] = read(f"sd_round_{r}.html")
        for n in (430, 431):
            for r in ("3073", "3074"):
                self.pages[f"https://scoring.dance/enUS/events/{n}/results/{r}.html"] = read(f"sd_round_{r}.html")

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.pages:
            raise RuntimeError(f"HTTP Error 404: Not Found ({url})")
        return self.pages[url]


class TestPendingNumbersAreRechecked(unittest.TestCase):
    def test_event_with_no_results_at_backfill_is_picked_up_later(self):
        conn = mem()
        sd = FakeSD()
        old = websites.SD_MISS_LIMIT
        websites.SD_MISS_LIMIT = 3
        try:
            rep = scan_scoring_dance(conn, start=BOOGIE, fetch=sd, now=D_BACKFILL, delay=0, index=False)
            self.assertEqual(rep["no_results"], 1)  # Boogie exists, nothing posted yet
            # Add 40 newer events so Boogie falls outside the "last 30 numbers" window.
            for n in range(432, 472):
                sd.pages[sd.idx(n)] = with_results(n, "12/01/2026", f"Later Event {n}")
            scan_scoring_dance(conn, start=432, end=471, fetch=sd, now=D_BACKFILL, delay=0, index=False)
            sd.post_results()
            rep = scan_scoring_dance(conn, fetch=sd, now=D_AFTER, delay=0)
        finally:
            websites.SD_MISS_LIMIT = old
        self.assertIn(sd.idx(BOOGIE), sd.calls[-200:])
        pe = conn.execute("SELECT name, start_date FROM provider_events WHERE provider='scoring_dance' "
                          "AND provider_key=?", (str(BOOGIE),)).fetchone()
        self.assertEqual(tuple(pe), ("Boogie By the Bay 2026", "2026-10-08"))
        out = dancer_results(conn, "Henry Leonard", include_registry=False)
        self.assertIn("Boogie By the Bay 2026", [e["event_name"] for e in out["events"]])

    def test_pending_check_at_most_daily(self):
        conn = mem()
        sd = FakeSD()
        scan_scoring_dance(conn, start=BOOGIE, end=BOOGIE, fetch=sd, now=D_BACKFILL, delay=0, index=False)
        conn.execute("INSERT INTO provider_events (provider, provider_key, name, links, fetched_at) "
                     "VALUES ('scoring_dance', '500', 'x', '[]', '2026-09-01T00:00:00Z')")
        n = len(sd.calls)
        scan_scoring_dance(conn, fetch=sd, now=D_BACKFILL + timedelta(hours=2), delay=0, index=False)
        self.assertNotIn(sd.idx(BOOGIE), sd.calls[n:])  # checked 2h ago: skip
        n = len(sd.calls)
        scan_scoring_dance(conn, fetch=sd, now=D_BACKFILL + timedelta(days=1), delay=0, index=False)
        self.assertIn(sd.idx(BOOGIE), sd.calls[n:])     # next day: recheck


class TestNamedLinks(unittest.TestCase):
    def test_named_link_recognized_and_resolved(self):
        self.assertEqual(scoringdance.event_number("https://scoring.dance/events/bbtb2024/results"), "bbtb2024")
        conn = mem()
        page = with_results(274, "10/16/2024", "Boogie By the Bay 2024")
        reg = add_scoring_dance_event(conn, "bbtb2024", fetch=lambda u: page if "bbtb2024" in u else (_ for _ in ()).throw(
            RuntimeError("404")), now=D_AFTER)
        self.assertEqual((reg["status"], reg["number"], reg["rounds"]), ("registered", "274", 2))
        keys = [r[0] for r in conn.execute("SELECT provider_key FROM provider_events")]
        self.assertEqual(keys, ["274"])


class TestWebsiteRecheckedAfterEvent(unittest.TestCase):
    def test_recent_event_site_checked_again_next_day(self):
        conn = mem()
        ev = import_external_event(conn, {"source": "manual", "name": "Boogie By The Bay",
                                          "start_date": "2026-10-08", "end_date": "2026-10-11"}, now=D_EVENT)
        conn.execute("UPDATE events SET website_url='https://boogie.example/' WHERE id=?", (ev["event_id"],))
        site = {"html": "<html><a href='/results'>Results</a></html>"}
        results_page = {"html": "<html><p>Coming soon</p></html>"}
        pages = {"https://boogie.example/": lambda: site["html"],
                 "https://boogie.example/results": lambda: results_page["html"]}
        sd_pages = {}

        def fetch(url):
            if url in pages:
                return pages[url]()
            if url in sd_pages:
                return sd_pages[url]
            raise RuntimeError("404")

        r1 = discover_from_websites(conn, fetch=fetch, now=D_EVENT, delay=0, index=False)
        self.assertEqual((r1["events_checked"], r1["provider_events_found"]), (1, 0))
        # Results get posted on the event site two days later.
        results_page["html"] = "<html><a href='https://scoring.dance/enUS/events/420/results/'>2026 results</a></html>"
        sd_pages["https://scoring.dance/enUS/events/420/results/"] = with_results(BOOGIE)
        r2 = discover_from_websites(conn, fetch=fetch, now=D_EVENT + timedelta(days=2), delay=0, index=False)
        self.assertEqual(r2["provider_events_found"], 1)
        found = r2["events"][0]["found"][0]
        self.assertEqual((found["provider"], found["event_id"]), ("scoring_dance", ev["event_id"]))

    def test_old_event_still_weekly(self):
        conn = mem()
        ev = import_external_event(conn, {"source": "manual", "name": "Old Fest",
                                          "start_date": "2026-05-01", "end_date": "2026-05-03"}, now=D_EVENT)
        conn.execute("UPDATE events SET website_url='https://old.example/' WHERE id=?", (ev["event_id"],))
        fetch = lambda u: "<html></html>"
        discover_from_websites(conn, fetch=fetch, now=D_EVENT, delay=0, index=False)
        r = discover_from_websites(conn, fetch=fetch, now=D_EVENT + timedelta(days=2), delay=0, index=False)
        self.assertEqual(r["events_checked"], 0)
