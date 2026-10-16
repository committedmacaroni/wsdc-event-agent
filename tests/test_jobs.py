import json
import unittest
from datetime import datetime, timezone

from wsdc_catalog.dancers import dancer_results
from wsdc_catalog.jobs import JobAlreadyRunning, daily_due, last_daily, run_daily
from tests.helpers import day1_fetch, mem
from tests.test_boogie_regression import BOOGIE, FakeSD
from tests.test_scoresheets import PAGES

NOW = datetime(2026, 10, 16, 17, tzinfo=timezone.utc)


def fetcher(extra=None):
    pages = {**PAGES, **(extra or {})}
    sd = FakeSD()
    sd.post_results()

    def f(url):
        if url in pages:
            return pages[url]
        return sd(url)
    return f


class TestDailyImport(unittest.TestCase):
    def test_one_call_imports_everything(self):
        conn = mem()
        # Production state: earlier scoring.dance numbers were already backfilled.
        conn.execute("INSERT INTO provider_events (provider, provider_key, name, start_date, links, fetched_at) "
                     "VALUES ('scoring_dance', '410', 'Earlier Event', '2026-01-10', '[]', '2026-02-01T00:00:00Z')")
        out = run_daily(conn, calendar_fetch=day1_fetch(), fetch=fetcher(), now=NOW, delay=0)
        steps = {s["step"]: s for s in out["steps"]}
        self.assertEqual(list(steps), ["calendar", "eepro", "scoring_dance", "websites", "cleanup"])
        self.assertEqual(steps["calendar"]["status"], "ok")
        self.assertEqual(steps["eepro"]["2026"]["events"], 3)
        self.assertGreaterEqual(steps["scoring_dance"]["new_events"], 1)
        # Score sheets from both providers are searchable after one call.
        self.assertEqual(dancer_results(conn, "Emily Madden", include_registry=False)["total_events"], 1)
        # Boogie's scoring.dance results attach to the WSDC calendar's existing event (no duplicate).
        evs = dancer_results(conn, "Henry Leonard", include_registry=False)["events"]
        boogie = [e for e in evs if e["event_name"] == "Boogie By The Bay"]
        self.assertEqual((len(boogie), boogie[0]["start_date"]), (1, "2026-10-08"))
        self.assertEqual(last_daily(conn)["run_id"], out["run_id"])

    def test_failed_source_does_not_stop_others(self):
        conn = mem()

        def broken_calendar():
            raise RuntimeError("worldsdc.com timed out")
        out = run_daily(conn, calendar_fetch=broken_calendar, fetch=fetcher(), now=NOW, delay=0)
        steps = {s["step"]: s for s in out["steps"]}
        self.assertEqual(out["status"], "failed")
        self.assertEqual(steps["calendar"]["status"], "failed")
        self.assertEqual(steps["eepro"]["status"], "ok")
        self.assertEqual(steps["scoring_dance"]["status"], "ok")
        self.assertIn("calendar", json.loads(conn.execute(
            "SELECT errors FROM sync_runs WHERE id=?", (out["run_id"],)).fetchone()[0])[0])

    def test_one_run_at_a_time(self):
        conn = mem()
        conn.execute("INSERT INTO sync_runs (source, started_at, status) VALUES ('job:daily', ?, 'running')",
                     ("2026-10-16T16:30:00Z",))
        with self.assertRaises(JobAlreadyRunning):
            run_daily(conn, calendar_fetch=day1_fetch(), fetch=fetcher(), now=NOW, delay=0)

    def test_due(self):
        conn = mem()
        from datetime import timedelta
        self.assertTrue(daily_due(conn, timedelta(hours=24), NOW))
        run_daily(conn, calendar_fetch=day1_fetch(), fetch=fetcher(), now=NOW, delay=0)
        self.assertFalse(daily_due(conn, timedelta(hours=24), NOW))
