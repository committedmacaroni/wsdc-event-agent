import json
import unittest
from datetime import datetime, timezone

from wsdc_catalog.dancers import dancer_results
from wsdc_catalog.judges import add_override, alias_map, list_judges, rebuild
from wsdc_catalog.scoresheets import index_provider_events
from wsdc_catalog.websites import add_scoring_dance_event
from tests.helpers import mem
from tests.test_providers_websites import NOW, fetcher

TS = "2026-09-29T00:00:00Z"


def add_panel(conn, sheet, section, judges, dancer="Test Dancer"):
    conn.execute("INSERT OR IGNORE INTO scoresheet_sheets (url, provider, status, fetched_at) "
                 "VALUES (?, 'eepro', 'parsed', ?)", (sheet, TS))
    conn.execute("INSERT INTO scoresheet_entries (sheet_url, provider, section, round, competitor_name, "
                 "normalized_name, marks) VALUES (?, 'eepro', ?, 'prelims', ?, ?, ?)",
                 (sheet, section, dancer, dancer.lower(), json.dumps({j: "Y" for j in judges})))


class TestJudgeMatching(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        c = self.conn
        add_panel(c, "s1", "A", ["Lisa M Picard", "Mike Smith", "Naomi", "Tren", "Chris Ng"])
        add_panel(c, "s2", "B", ["Lisa Picard", "Michael Smith", "Naomi Uyama", "Trendlyon Veal", "Christopher Ng"])
        add_panel(c, "s3", "C", ["Naomi Jones", "Chris Ng", "Christopher Ng"])  # both Ngs on one panel
        rebuild(c)
        self.m = alias_map(c)

    def key(self, name):
        return self.m[name.lower().replace(".", "")]["judge_key"]

    def test_middle_name_and_nickname_high(self):
        self.assertEqual(self.key("Lisa M Picard"), self.key("Lisa Picard"))
        self.assertEqual(self.m["lisa picard"]["confidence"], "high")
        self.assertEqual(self.key("Mike Smith"), self.key("Michael Smith"))
        self.assertEqual(self.m["mike smith"]["method"], "nickname")

    def test_first_name_only_unique_match_medium(self):
        self.assertEqual(self.key("Tren"), self.key("Trendlyon Veal"))
        self.assertEqual((self.m["tren"]["method"], self.m["tren"]["confidence"]), ("first_name_only", "medium"))
        self.assertEqual(self.m["tren"]["judge_name"], "Trendlyon Veal")

    def test_ambiguous_first_name_needs_review(self):
        self.assertEqual(self.m["naomi"]["method"], "needs_review")
        self.assertNotEqual(self.key("Naomi"), self.key("Naomi Uyama"))
        review = list_judges(self.conn, review_only=True)["needs_review"]
        self.assertEqual(review[0]["candidates"], ["Naomi Jones", "Naomi Uyama"])

    def test_same_panel_never_merged(self):
        self.assertNotEqual(self.key("Chris Ng"), self.key("Christopher Ng"))

    def test_manual_overrides(self):
        add_override(self.conn, "merge", "Naomi", "Naomi Uyama")
        m = alias_map(self.conn)
        self.assertEqual(m["naomi"]["judge_key"], m["naomi uyama"]["judge_key"])
        self.assertEqual(m["naomi"]["method"], "manual")
        add_override(self.conn, "separate", "Lisa M Picard", "Lisa Picard")
        m = alias_map(self.conn)
        self.assertNotEqual(m["lisa m picard"]["judge_key"], m["lisa picard"]["judge_key"])
        with self.assertRaises(ValueError):
            add_override(self.conn, "combine", "a", "b")


class TestRealScoringDanceJudges(unittest.TestCase):
    """Swing Resolution 2025: 'Tren' on the prelim panel, 'Trendlyon Veal' on the final panel."""

    def test_tren_is_trendlyon_veal(self):
        conn = mem()
        add_scoring_dance_event(conn, "195", fetch=fetcher(), now=NOW)
        index_provider_events(conn, provider="scoring_dance", fetch=fetcher(), now=NOW, delay=0)  # rebuilds
        m = alias_map(conn)
        self.assertEqual(m["tren"]["judge_name"], "Trendlyon Veal")
        # Ellen's prelim + final: Markus Smith judged both, and appears once with both events counted
        out = dancer_results(conn, "Ellen Dacombe", include_registry=False)
        judges = {j["judge"]: j for j in out["judges"]}
        self.assertEqual(len(judges["Markus Smith"]["history"]), 2)
        self.assertEqual(judges["Markus Smith"]["listed_as"][0]["method"], "exact")
