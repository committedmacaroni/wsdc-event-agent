"""The daily import: every step that brings in new events and score sheets, in order, as one job.

Steps (each runs even if an earlier one fails; failures are reported, not raised):
  1. calendar     WSDC event calendar sync
  2. eepro        Event Express Pro: this year's events and new score sheets
                  (in January also last year's, for late-December events)
  3. scoring_dance  scoring.dance: new event numbers, plus numbers that had no results yet
  4. websites     event websites: results links posted after the event (eepro, scoring.dance, WDR)
  5. cleanup      merge cross-source duplicate events, refresh judge name matching

Run it with POST /admin/jobs/daily (what the GitHub Actions schedule calls), or
`python -m wsdc_catalog daily`. One run at a time; the run is recorded in sync_runs as
source "job:daily", with each step's result in `changes`.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from .db import iso, utcnow
from .fetcher import fetch_html

SOURCE = "job:daily"
STALE_AFTER = timedelta(hours=3)


class JobAlreadyRunning(Exception):
    pass


def _claim(conn, now: datetime) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        running = conn.execute("SELECT id, started_at FROM sync_runs WHERE source=? AND status='running'",
                               (SOURCE,)).fetchone()
        if running:
            started = datetime.fromisoformat(running["started_at"].replace("Z", "+00:00"))
            if now - started < STALE_AFTER:
                raise JobAlreadyRunning(f"daily import {running['id']} has been running since {running['started_at']}")
            conn.execute("UPDATE sync_runs SET status='failed', completed_at=? WHERE id=?", (iso(now), running["id"]))
        run_id = conn.execute("INSERT INTO sync_runs (source, started_at, status) VALUES (?,?, 'running')",
                              (SOURCE, iso(now))).lastrowid
        conn.execute("COMMIT")
        return run_id
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def run_daily(conn, *, calendar_fetch, fetch=fetch_html, now: datetime | None = None,
              snapshot_dir=None, calendar_url: str | None = None, delay: float | None = None,
              website_limit: int = 25) -> dict:
    """Run the full daily import. Returns {"run_id", "status", "steps": [...], "duration_s"}."""
    from .dedupe import merge_duplicates
    from .judges import rebuild
    from .scoresheets import index_eepro_year
    from .sync import run_wsdc_sync
    from .websites import discover_from_websites, scan_scoring_dance

    now = now or utcnow()
    run_id = _claim(conn, now)
    t0 = time.monotonic()
    kw = {} if delay is None else {"delay": delay}
    years = [now.year] + ([now.year - 1] if now.month == 1 else [])

    def step_calendar():
        r = run_wsdc_sync(conn, calendar_fetch, now=now, snapshot_dir=snapshot_dir, source_url=calendar_url)
        if r["status"] != "success":
            raise RuntimeError(f"calendar sync {r['status']}: {'; '.join(r['errors'][-2:])}")
        return {k: r[k] for k in ("records_found", "records_added", "records_updated", "records_deactivated")}

    def step_eepro():
        out = {}
        for y in years:
            r = index_eepro_year(conn, y, fetch=fetch, now=now, **kw)
            if r["status"] != "success":
                raise RuntimeError(f"eepro {y}: {'; '.join(r['errors'][-2:])}")
            out[str(y)] = {k: r.get(k) for k in ("events", "sheets_indexed", "sheets_failed")}
        return out

    def step_scoring_dance():
        r = scan_scoring_dance(conn, fetch=fetch, now=now, **kw)
        idx = r.get("indexing") or {}
        return {"new_events": r["registered"], "still_waiting": r["no_results"], "range": r["scanned_range"],
                "sheets_indexed": idx.get("sheets_indexed"), "errors": r["errors"][:5]}

    def step_websites():
        r = discover_from_websites(conn, fetch=fetch, now=now, limit=website_limit, **kw)
        return {"sites_checked": r["events_checked"], "results_found": r["provider_events_found"],
                "other_links": r["other_results_links"], "errors": r["errors"][:5]}

    def step_cleanup():
        d = merge_duplicates(conn)
        j = rebuild(conn)
        return {"events_merged": d["merged"], "judges": j["judges"], "judges_needing_review": j["needs_review"]}

    steps = []
    for name, fn in (("calendar", step_calendar), ("eepro", step_eepro), ("scoring_dance", step_scoring_dance),
                     ("websites", step_websites), ("cleanup", step_cleanup)):
        s0 = time.monotonic()
        try:
            steps.append({"step": name, "status": "ok", **fn()})
        except Exception as e:  # noqa: BLE001 - one failed source shouldn't stop the others
            steps.append({"step": name, "status": "failed", "error": f"{type(e).__name__}: {e}"[:500]})
        steps[-1]["seconds"] = round(time.monotonic() - s0, 1)

    status = "success" if all(s["status"] == "ok" for s in steps) else "failed"
    errors = [f"{s['step']}: {s['error']}" for s in steps if s["status"] == "failed"]
    conn.execute("UPDATE sync_runs SET status=?, completed_at=?, errors=?, changes=? WHERE id=?",
                 (status, iso(utcnow()), json.dumps(errors), json.dumps(steps), run_id))
    return {"run_id": run_id, "status": status, "steps": steps, "duration_s": round(time.monotonic() - t0, 1)}


def last_daily(conn) -> dict | None:
    r = conn.execute("SELECT id, status, started_at, completed_at, errors FROM sync_runs WHERE source=? "
                     "AND status <> 'running' ORDER BY id DESC LIMIT 1", (SOURCE,)).fetchone()
    if not r:
        return None
    return {"run_id": r["id"], "status": r["status"], "started_at": r["started_at"],
            "completed_at": r["completed_at"], "errors": json.loads(r["errors"] or "[]")}


def daily_due(conn, interval: timedelta, now: datetime) -> bool:
    r = conn.execute("SELECT MAX(started_at) FROM sync_runs WHERE source=? AND status='success'",
                     (SOURCE,)).fetchone()
    if not r or not r[0]:
        return True
    return now - datetime.fromisoformat(r[0].replace("Z", "+00:00")) >= interval
