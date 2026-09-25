"""CLI: python -m wsdc_catalog {init-db,sync,serve,runs,events}"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from .api import serve
from .config import Config
from .db import connect
from .fetcher import fetch_wsdc, file_fetcher
from .queries import list_events, list_sync_runs
from .registry import lookup_competitor, search_competitors
from .scheduler import DailySyncScheduler
from .sync import SyncAlreadyRunning, run_wsdc_sync


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="wsdc_catalog")
    p.add_argument("--db", help="SQLite path (default $CATALOG_DB_PATH or data/catalog.sqlite3)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    s = sub.add_parser("sync", help="run one WSDC sync")
    s.add_argument("--from-file", help="parse saved HTML instead of fetching")
    s.add_argument("--confirmed-file", help="saved ?hideunconfirmed=1 HTML")
    s.add_argument("--force", action="store_true", help="override the safety guard")
    s.add_argument("--now", help="ISO datetime to treat as 'now' (testing/replay)")
    s.add_argument("--quiet", action="store_true", help="print counts only")
    v = sub.add_parser("serve")
    v.add_argument("--host", default="0.0.0.0")
    v.add_argument("--port", type=int, default=8080)
    v.add_argument("--schedule", action="store_true", help="enable the daily sync scheduler")
    r = sub.add_parser("runs")
    r.add_argument("--limit", type=int, default=5)
    c = sub.add_parser("competitor", help="look up a dancer by WSDC ID or search by name")
    c.add_argument("query", help="WSDC ID (digits) or a name")
    e = sub.add_parser("events")
    e.add_argument("params", nargs="*", help="filters as key=value, e.g. year=2026 country=USA")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config()
    if args.db:
        cfg.db_path = args.db

    if args.cmd == "init-db":
        connect(cfg.db_path).close()
        print(f"schema ready at {cfg.db_path}")
    elif args.cmd == "sync":
        conn = connect(cfg.db_path)
        fetch = (file_fetcher(args.from_file, args.confirmed_file, cfg.wsdc_url) if args.from_file
                 else (lambda: fetch_wsdc(cfg.wsdc_url)))
        now = datetime.fromisoformat(args.now).replace(tzinfo=timezone.utc) if args.now else None
        try:
            run = run_wsdc_sync(conn, fetch, now=now, force=args.force, snapshot_dir=cfg.snapshot_dir,
                                source_url=cfg.wsdc_url)
        except SyncAlreadyRunning as ex:
            print(f"error: {ex}", file=sys.stderr)
            return 2
        if args.quiet:
            run = {k: run[k] for k in run if k.startswith("records_") or k in ("id", "status", "errors")}
        _print(run)
        return 0 if run["status"] == "success" else 1
    elif args.cmd == "serve":
        server = serve(cfg, args.host, args.port)
        if args.schedule:
            DailySyncScheduler(cfg).start()
        logging.info("listening on %s:%s (scheduler %s)", args.host, args.port,
                     "on" if args.schedule else "off")
        server.serve_forever()
    elif args.cmd == "runs":
        _print(list_sync_runs(connect(cfg.db_path), args.limit))
    elif args.cmd == "competitor":
        conn = connect(cfg.db_path)
        q = args.query.strip()
        _print(lookup_competitor(conn, q) if q.isdigit() else search_competitors(conn, q))
    elif args.cmd == "events":
        _print(list_events(connect(cfg.db_path), dict(kv.split("=", 1) for kv in args.params)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
