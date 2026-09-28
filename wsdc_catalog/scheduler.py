"""Daily sync scheduler.

Polls every 15 minutes and syncs when the last successful run is older than the
interval, so process restarts or host sleep/wake neither double-sync nor skip a day.
If the host suspends idle processes entirely, use an external cron hitting
POST /admin/sync-events instead.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from .config import Config
from .db import connect, utcnow
from .fetcher import fetch_wsdc
from .scoresheets import discover_eepro_year
from .sync import SyncAlreadyRunning, run_wsdc_sync

log = logging.getLogger("wsdc_catalog.scheduler")
POLL_SECONDS = 15 * 60


def sync_due(conn, interval: timedelta, now: datetime) -> bool:
    r = conn.execute("SELECT started_at FROM sync_runs WHERE source='wsdc_calendar' AND status='success' "
                     "ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return True
    last = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
    return now - last >= interval


class DailySyncScheduler(threading.Thread):
    def __init__(self, cfg: Config, fetcher=None):
        super().__init__(daemon=True, name="wsdc-sync-scheduler")
        self.cfg = cfg
        self.fetcher = fetcher or (lambda: fetch_wsdc(cfg.wsdc_url))
        self.interval = timedelta(hours=cfg.sync_interval_hours)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            conn = connect(self.cfg.db_path)
            try:
                if sync_due(conn, self.interval, utcnow()):
                    run = run_wsdc_sync(conn, self.fetcher, snapshot_dir=self.cfg.snapshot_dir,
                                        source_url=self.cfg.wsdc_url)
                    log.info("scheduled sync %s: %s found=%s added=%s updated=%s deactivated=%s",
                             run["id"], run["status"], run["records_found"], run["records_added"],
                             run["records_updated"], run["records_deactivated"])
                    disc = discover_eepro_year(conn, utcnow().year)
                    log.info("eepro event discovery: %s, %s events", disc["status"], disc["events"])
            except SyncAlreadyRunning:
                log.info("scheduled sync skipped: another run in progress")
            except Exception:  # noqa: BLE001
                log.exception("scheduled sync crashed")
            finally:
                conn.close()
            self._stop.wait(POLL_SECONDS)
