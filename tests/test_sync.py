import json
import unittest

from wsdc_catalog.fetcher import file_fetcher
from wsdc_catalog.sync import SyncAlreadyRunning, run_wsdc_sync
from tests.helpers import DAY1, DAY2, day1_fetch, day2_fetch, fx, mem


def ev(conn, name, start=None):
    sql, args = "SELECT * FROM events WHERE name=?", [name]
    if start:
        sql += " AND start_date=?"; args.append(start)
    return conn.execute(sql, args).fetchone()


class TestSync(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        self.run1 = run_wsdc_sync(self.conn, day1_fetch(), now=DAY1)

    def test_first_sync_counts(self):
        r = self.run1
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["records_found"], 36)
        self.assertEqual(r["records_skipped"], 1)  # duplicate Waterloo listing
        self.assertEqual(r["records_added"], 35)
        self.assertEqual(r["records_updated"], 0)

    def test_series_and_aliases(self):
        n = self.conn.execute("SELECT COUNT(*) FROM events e JOIN event_series s ON s.id=e.series_id "
                              "WHERE s.canonical_name='Atlanta Swing Classic'").fetchone()[0]
        self.assertEqual(n, 3)  # one series, three yearly occurrences
        tap = self.conn.execute("SELECT id FROM event_series WHERE canonical_name='The After Party'").fetchone()
        aliases = {r[0] for r in self.conn.execute("SELECT alias FROM event_aliases WHERE series_id=?", (tap[0],))}
        self.assertEqual(aliases, {"The After Party aka TAP", "TAP"})

    def test_same_series_distinct_occurrences(self):
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE name='WCS Festival'").fetchone()[0], 2)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE name='Swingside Invitational'").fetchone()[0], 2)

    def test_confirmation_status(self):
        self.assertEqual(ev(self.conn, "Warsaw Halloween Swing")["confirmation_status"], "unconfirmed")
        self.assertEqual(ev(self.conn, "Boogie By The Bay")["confirmation_status"], "confirmed")

    def test_idempotent(self):
        r = run_wsdc_sync(self.conn, day1_fetch(), now=DAY1)
        self.assertEqual((r["records_added"], r["records_updated"], r["records_unchanged"]), (0, 0, 35))

    def test_second_day_changes(self):
        boogie_id = ev(self.conn, "Boogie By The Bay")["id"]
        r = run_wsdc_sync(self.conn, day2_fetch(), now=DAY2)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["records_added"], 1)            # Fixture Swing Weekend
        self.assertEqual(r["records_updated"], 3)          # Boogie dates, Swiss status, Warsaw status+confirmed
        self.assertEqual(r["records_deactivated"], 2)      # Aloha (past), Honey Fest (missing)
        boogie = ev(self.conn, "Boogie By The Bay")
        self.assertEqual((boogie["id"], boogie["start_date"]), (boogie_id, "2026-10-09"))  # same id kept
        self.assertEqual(ev(self.conn, "Swiss Open WCS")["event_status"], "registry")
        warsaw = ev(self.conn, "Warsaw Halloween Swing")
        self.assertEqual((warsaw["event_status"], warsaw["confirmation_status"]), ("registry", "confirmed"))
        aloha = ev(self.conn, "The Aloha Open")
        self.assertEqual((aloha["active"], aloha["lifecycle_status"]), (0, "past"))
        honey = ev(self.conn, "HONEY FEST")
        self.assertEqual((honey["active"], honey["lifecycle_status"]), (0, "missing_from_source"))
        obs = self.conn.execute("SELECT previous_values FROM event_observations WHERE event_id=? "
                                "AND previous_values IS NOT NULL", (boogie_id,)).fetchone()
        self.assertEqual(json.loads(obs[0])["start_date"], "2026-10-08")

    def test_reappearing_event_reactivated(self):
        run_wsdc_sync(self.conn, day2_fetch(), now=DAY2)
        r = run_wsdc_sync(self.conn, day1_fetch(), now=DAY2)
        self.assertEqual(ev(self.conn, "HONEY FEST")["active"], 1)
        self.assertEqual(r["status"], "success")

    def test_guard_blocks_broken_layout(self):
        r = run_wsdc_sync(self.conn, file_fetcher(fx("wsdc_broken.html")), now=DAY2)
        self.assertEqual(r["status"], "aborted_guard")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE active=1").fetchone()[0], 35)

    def test_guard_blocks_partial_parse_unless_forced(self):
        r = run_wsdc_sync(self.conn, file_fetcher(fx("wsdc_partial.html")), now=DAY2)
        self.assertEqual(r["status"], "aborted_guard")
        r = run_wsdc_sync(self.conn, file_fetcher(fx("wsdc_partial.html")), now=DAY2, force=True)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["records_deactivated"], 30)

    def test_missing_confirmed_list_does_not_flap(self):
        r = run_wsdc_sync(self.conn, file_fetcher(fx("wsdc_day1.html")), now=DAY1)
        self.assertEqual(r["records_updated"], 0)
        self.assertEqual(ev(self.conn, "Boogie By The Bay")["confirmation_status"], "confirmed")

    def test_lock(self):
        self.conn.execute("INSERT INTO sync_runs (source, started_at, status) "
                          "VALUES ('wsdc_calendar', '2026-09-24T11:30:00Z', 'running')")
        with self.assertRaises(SyncAlreadyRunning):
            run_wsdc_sync(self.conn, day1_fetch(), now=DAY1)

    def test_stale_lock_released(self):
        self.conn.execute("INSERT INTO sync_runs (source, started_at, status) "
                          "VALUES ('wsdc_calendar', '2026-09-24T01:00:00Z', 'running')")
        self.assertEqual(run_wsdc_sync(self.conn, day1_fetch(), now=DAY1)["status"], "success")

    def test_fetch_failure_recorded(self):
        def boom():
            raise RuntimeError("fetch failed for https://worldsdc.com/events/: timed out")
        r = run_wsdc_sync(self.conn, boom, now=DAY2)
        self.assertEqual(r["status"], "failed")
        self.assertIn("timed out", r["errors"][-1])
