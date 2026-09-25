"""Configuration from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    db_path: str = os.environ.get("CATALOG_DB_PATH", "data/catalog.sqlite3")
    snapshot_dir: str = os.environ.get("CATALOG_SNAPSHOT_DIR", "data/snapshots")
    wsdc_url: str = os.environ.get("WSDC_EVENTS_URL", "https://worldsdc.com/events/")
    catalog_api_key: str | None = os.environ.get("CATALOG_API_KEY") or None
    admin_api_key: str | None = os.environ.get("ADMIN_API_KEY") or None
    sync_interval_hours: float = float(os.environ.get("SYNC_INTERVAL_HOURS", "24"))
