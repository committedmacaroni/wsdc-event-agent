"""In-process daily import (backup only).

Sprites pause when idle, so this timer can't be relied on. The daily import is triggered by
the GitHub Actions schedule (.github/workflows/daily-import.yml) calling POST /admin/jobs/daily;
this thread only covers the case where the service happens to be awake and the import is overdue.
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta

from .config import Config
from .db import connect, utcnow
from .fetcher import fetch_wsdc
from .jobs import JobAlreadyRunning, daily_due, run_daily

log = logging.getLogger("wsdc_catalog.scheduler")
POLL_SECONDS = 15 * 60


class DailySyncScheduler(threading.Thread):
    def __init__(self, cfg: Config, fetcher=None):
        super().__init__(daemon=True, name="wsdc-daily-import")
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
                if daily_due(conn, self.interval, utcnow()):
                    out = run_daily(conn, calendar_fetch=self.fetcher, snapshot_dir=self.cfg.snapshot_dir,
                                    calendar_url=self.cfg.wsdc_url)
                    log.info("daily import %s: %s in %ss", out["run_id"], out["status"], out["duration_s"])
            except JobAlreadyRunning:
                log.info("daily import skipped: another run in progress")
            except Exception:  # noqa: BLE001
                log.exception("daily import crashed")
            finally:
                conn.close()
            self._stop.wait(POLL_SECONDS)
