import unittest

from wsdc_catalog.parser import parse_event_list
from tests.helpers import fx


def load(name):
    with open(fx(name), encoding="utf-8") as f:
        return parse_event_list(f.read())


class TestParser(unittest.TestCase):
    def setUp(self):
        self.res = load("wsdc_day1.html")
        self.by_name = {}
        for r in self.res.rows:
            self.by_name.setdefault(r.name, []).append(r)

    def test_all_rows_parse(self):
        self.assertTrue(self.res.table_found)
        self.assertEqual(len(self.res.rows), 36)
        self.assertEqual(self.res.errors, [])

    def test_status_labels(self):
        self.assertEqual(self.by_name["RiverSwingNights"][0].event_status, "trial")
        self.assertEqual(self.by_name["Boogie By The Bay"][0].event_status, "registry")
        self.assertEqual(self.by_name["The Open Swing Dance Championships"][0].event_status, "unknown")

    def test_country_sources(self):
        self.assertEqual(self.by_name["Moscow Westie Fest Gala Edition"][0].country, "RUS")  # transparent flag
        self.assertEqual(self.by_name["New York Flow Festival"][0].country, "USA")           # empty flag
        waterloo = self.by_name["Waterloo Ontario Open West Coast Swing Championships"]
        self.assertEqual([w.country for w in waterloo], ["CAN", "CAN"])                     # USA flag overridden
        self.assertTrue(waterloo[0].warnings)

    def test_hiatus_and_urls(self):
        soul = next(r for r in self.res.rows if r.name.startswith("Soul Flow"))
        self.assertTrue(soul.hiatus)
        self.assertEqual(self.by_name["Paris Swing Classic"][0].website_url, "Https://parisswingclassic.com")

    def test_layout_change_detected(self):
        res = load("wsdc_broken.html")
        self.assertFalse(res.table_found)
        self.assertEqual(res.rows, [])
