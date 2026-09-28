import unittest
from datetime import date

from wsdc_catalog.normalize import (normalize_country_filter, normalize_event_name, parse_date_range,
                                    parse_location, split_event_name)


class TestNames(unittest.TestCase):
    def test_aka(self):
        self.assertEqual(split_event_name("The After Party aka TAP"),
                         ("The After Party", ["The After Party aka TAP", "TAP"]))

    def test_parenthetical_and_quoted(self):
        self.assertEqual(split_event_name("MADjam (Mid Atlantic Dance Jam)")[0], "MADjam")
        self.assertEqual(split_event_name('Scandinavian Open WCS "SNOW"'),
                         ("Scandinavian Open WCS", ['Scandinavian Open WCS "SNOW"', "SNOW"]))

    def test_year_and_hiatus_removed(self):
        self.assertEqual(normalize_event_name("Mooseland Swing 2026"), "mooseland swing")
        self.assertEqual(split_event_name("Soul Flow - WCS Festival (Hiatus -- 2026)")[0], "Soul Flow - WCS Festival")
        self.assertEqual(normalize_event_name("5280 Westival"), "5280 westival")

    def test_apostrophe_not_treated_as_quote(self):
        self.assertEqual(split_event_name("Swingin' Into Spring"), ("Swingin' Into Spring", []))


class TestDates(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_date_range("Sep 23 - 27, 2026"), (date(2026, 9, 23), date(2026, 9, 27)))
        self.assertEqual(parse_date_range("Oct 28 - Nov 2, 2026"), (date(2026, 10, 28), date(2026, 11, 2)))
        self.assertEqual(parse_date_range("Dec 28 2026 - Jan 3 2027"), (date(2026, 12, 28), date(2027, 1, 3)))
        self.assertEqual(parse_date_range("Dec 30 - Jan 3, 2027"), (date(2026, 12, 30), date(2027, 1, 3)))
        self.assertEqual(parse_date_range("Feb 29 - Mar 5, 2028"), (date(2028, 2, 29), date(2028, 3, 5)))

    def test_rejects_garbage(self):
        for bad in ("TBD", "Sep 30 - 2, 2026", ""):
            with self.assertRaises(ValueError):
                parse_date_range(bad)


class TestLocation(unittest.TestCase):
    def test_messy_locations(self):
        self.assertEqual(parse_location("Liège, , Belgium")["country_code"], "BEL")
        loc = parse_location("Oslo, Norway, Oslo, Norway")
        self.assertEqual((loc["city"], loc["region"], loc["country_code"]), ("Oslo", None, "NOR"))
        loc = parse_location("Atlanta, GA USA")
        self.assertEqual((loc["region"], loc["country_code"]), ("GA", "USA"))
        self.assertIsNone(parse_location("Hartford, CT")["country_code"])

    def test_country_filter(self):
        for v in ("USA", "us", "United States"):
            self.assertEqual(normalize_country_filter(v), "USA")
