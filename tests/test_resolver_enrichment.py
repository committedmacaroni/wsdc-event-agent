import unittest

from wsdc_catalog.enrichment import import_external_event
from wsdc_catalog.resolver import prepare_find_results_request, resolve_event
from wsdc_catalog.sync import run_wsdc_sync
from tests.helpers import DAY1, day1_fetch, mem


class TestResolver(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        run_wsdc_sync(self.conn, day1_fetch(), now=DAY1)
        self.tap = self.conn.execute("SELECT id FROM events WHERE name='The After Party aka TAP'").fetchone()[0]

    def test_event_id_context(self):
        out = prepare_find_results_request(self.conn, {"event_id": self.tap})
        self.assertTrue(out["ok"])
        ctx = out["match_context"]
        self.assertEqual(ctx["canonical_name"], "The After Party")
        self.assertIn("TAP", ctx["search_terms"])
        self.assertEqual((ctx["start_date"], ctx["country"]), ("2026-12-03", "USA"))

    def test_unknown_event_id(self):
        self.assertEqual(prepare_find_results_request(self.conn, {"event_id": "evt_nope"})["status_code"], 404)

    def test_metadata_resolves(self):
        res = resolve_event(self.conn, metadata={"name": "The After Party", "year": 2026})
        self.assertEqual((res["status"], res["event"]["id"]), ("resolved", self.tap))

    def test_short_alias_needs_corroboration(self):
        res = resolve_event(self.conn, metadata={"name": "TAP"})
        self.assertNotEqual(res["status"], "resolved")
        res = resolve_event(self.conn, metadata={"name": "TAP", "start_date": "2026-12-03", "country": "US"})
        self.assertEqual(res["status"], "resolved")

    def test_multi_year_series_is_ambiguous_without_date(self):
        res = resolve_event(self.conn, metadata={"name": "Atlanta Swing Classic"})
        self.assertEqual(res["status"], "ambiguous")
        self.assertEqual(len(res["candidates"]), 3)
        res = resolve_event(self.conn, metadata={"name": "Atlanta Swing Classic", "year": 2027})
        self.assertEqual((res["status"], res["event"]["year"]), ("resolved", 2027))

    def test_uncatalogued(self):
        out = prepare_find_results_request(self.conn, {"name": "Totally New Swing Thing", "year": 2026})
        self.assertEqual((out["ok"], out["catalog_status"]), (True, "uncatalogued"))


class TestEnrichment(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        run_wsdc_sync(self.conn, day1_fetch(), now=DAY1)

    def test_historical_event_created_without_touching_canonical(self):
        out = import_external_event(self.conn, {
            "source": "scoring_dance", "name": "The After Party \"TAP\"", "start_date": "2019-12-05",
            "end_date": "2019-12-09", "country": "USA", "external_id": "tap-2019"})
        self.assertEqual(out["status"], "created")
        row = self.conn.execute("SELECT * FROM events WHERE id=?", (out["event_id"],)).fetchone()
        self.assertEqual((row["lifecycle_status"], row["active"], row["source"]), ("historical", 0, "scoring_dance"))
        s = self.conn.execute("SELECT canonical_name FROM event_series WHERE id=?", (row["series_id"],)).fetchone()
        self.assertEqual(s[0], "The After Party")

    def test_existing_occurrence_matched(self):
        out = import_external_event(self.conn, {
            "source": "scoring_dance", "name": "Boogie by the Bay 2026", "start_date": "2026-10-09",
            "country": "USA", "external_id": "bbb-2026", "url": "https://scoring.dance/x"})
        self.assertEqual(out["status"], "matched")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM event_external_refs").fetchone()[0], 1)

    def test_historical_survives_wsdc_sync(self):
        out = import_external_event(self.conn, {"source": "wsdc_competitor_record", "name": "Old Event",
                                                "start_date": "2015-05-01"})
        run_wsdc_sync(self.conn, day1_fetch(), now=DAY1)
        row = self.conn.execute("SELECT lifecycle_status FROM events WHERE id=?", (out["event_id"],)).fetchone()
        self.assertEqual(row[0], "historical")
