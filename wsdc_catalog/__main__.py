"""CLI: python -m wsdc_catalog {init-db,sync,serve,runs,events}"""
from __future__ import annotations

import argparse
import re
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
from .dancers import dancer_results
from .scoresheets import coverage, discover_eepro_year, index_eepro_year, lookup_for_events, search_by_name
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
    ss = sub.add_parser("scoresheets", help="score-sheet index")
    ssub = ss.add_subparsers(dest="ss_cmd", required=True)
    sd = ssub.add_parser("discover", help="add a provider's past events to the catalog (no score sheets)")
    sd.add_argument("--year", type=int, action="append", required=True, help="repeatable")
    sx = ssub.add_parser("index", help="download and index every sheet for a year (backfill; safe to re-run)")
    sx.add_argument("--year", type=int, action="append", required=True, help="repeatable")
    sw = ssub.add_parser("websites", help="scan event websites for results links (eepro, scoring.dance, other)")
    sw.add_argument("--limit", type=int, default=25)
    sw.add_argument("--event", action="append", help="only these catalog event ids (repeatable)")
    sd2 = ssub.add_parser("scoring-dance", help="register + index scoring.dance events by event number")
    sd2.add_argument("--number", action="append", required=True, help="the number in scoring.dance/.../events/<n>/")
    ss2 = ssub.add_parser("scoring-dance-scan", help="find every scoring.dance event by walking event numbers")
    ss2.add_argument("--from", dest="start", type=int, help="first event number (1 for a full backfill)")
    ss2.add_argument("--to", dest="end", type=int, help="last event number (default: stop after 40 missing)")
    sc = ssub.add_parser("capture", help="save a results page's HTML to data/captures/ and show how it parses")
    sc.add_argument("url")
    sl = ssub.add_parser("lookup", help="fetch sheets for selected events and find a dancer")
    sl.add_argument("name")
    sl.add_argument("--event", action="append", required=True, help="catalog event id (repeatable)")
    sq = ssub.add_parser("search", help="search sheets already retrieved (no provider requests)")
    sq.add_argument("name")
    sq.add_argument("--year", type=int)
    sp = ssub.add_parser("parse", help="debug: fetch one sheet URL and show what the parser sees")
    sp.add_argument("url")
    ssub.add_parser("coverage")
    d = sub.add_parser("dancer", help="everything for a dancer name (what Replit's search returns)")
    d.add_argument("name")
    d.add_argument("--wsdc-id")
    d.add_argument("--year", type=int)
    jg = sub.add_parser("judges", help="judge identities (name variants merged)")
    jsub = jg.add_subparsers(dest="j_cmd", required=True)
    jsub.add_parser("rebuild")
    jl = jsub.add_parser("list")
    jl.add_argument("--review", action="store_true", help="only names that need a human decision")
    for act in ("merge", "separate"):
        ja = jsub.add_parser(act)
        ja.add_argument("name_a")
        ja.add_argument("name_b")
    sub.add_parser("dedupe", help="merge the same event recorded under different names by different sources")
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
    elif args.cmd == "scoresheets":
        conn = connect(cfg.db_path)
        if args.ss_cmd == "discover":
            _print([discover_eepro_year(conn, y) for y in args.year])
        elif args.ss_cmd == "index":
            for y in args.year:
                _print(index_eepro_year(conn, y))
        elif args.ss_cmd == "lookup":
            _print(lookup_for_events(conn, args.name, args.event))
        elif args.ss_cmd == "search":
            _print(search_by_name(conn, args.name, year=args.year))
        elif args.ss_cmd == "coverage":
            _print(coverage(conn))
        elif args.ss_cmd == "websites":
            from .websites import discover_from_websites
            _print(discover_from_websites(conn, limit=args.limit, event_ids=args.event))
        elif args.ss_cmd == "scoring-dance-scan":
            from .websites import scan_scoring_dance
            rep = scan_scoring_dance(conn, start=args.start, end=args.end)
            rep["events"] = rep["events"][-20:]
            _print(rep)
        elif args.ss_cmd == "scoring-dance":
            from .scoresheets import index_provider_events
            from .websites import add_scoring_dance_event
            for n in args.number:
                reg = add_scoring_dance_event(conn, n)
                _print(reg)
            _print(index_provider_events(conn, provider="scoring_dance"))
        elif args.ss_cmd in ("parse", "capture"):
            from pathlib import Path
            from urllib.parse import urlparse
            from . import eepro, scoringdance
            from .fetcher import fetch_html
            url = scoringdance.to_en(args.url)
            html = fetch_html(url)
            if args.ss_cmd == "capture":
                d = Path("data/captures"); d.mkdir(parents=True, exist_ok=True)
                name = re.sub(r"[^A-Za-z0-9]+", "_", urlparse(url).netloc + urlparse(url).path).strip("_")[:120]
                (d / f"{name}.html").write_text(html, encoding="utf-8")
                print(f"saved {d / (name + '.html')} ({len(html)} bytes)")
            parser = scoringdance if "scoring.dance" in url else eepro
            r = parser.parse_sheet(html)
            print(f"event: {r['event_title']}  sections: {len(r['sections'])}  errors: {len(r['errors'])}")
            for sec in r["sections"]:
                e0 = sec["entries"][0]
                print(f"  {sec['title']!r}: {sec['division']}/{sec['role'] or 'couples'}/{sec['round']}, "
                      f"{len(sec['judges'])} judges named, {len(sec['entries'])} entries; first: "
                      f"{e0['name']} bib {e0['bib']} marks {e0['marks']}")
            for err in r["errors"][:10]:
                print("  ERROR", err)
    elif args.cmd == "dancer":
        _print(dancer_results(connect(cfg.db_path), args.name, wsdc_id=args.wsdc_id, year=args.year))
    elif args.cmd == "judges":
        from .judges import add_override, list_judges, rebuild
        conn = connect(cfg.db_path)
        if args.j_cmd == "rebuild":
            _print(rebuild(conn))
        elif args.j_cmd == "list":
            out = list_judges(conn, review_only=args.review)
            for j in out["judges"]:
                variants = ", ".join(f"{n['name']} [{n['method']}/{n['confidence']}]" for n in j["names"])
                print(f"{j['judge']}  ({j['panels']} panels): {variants}")
            for r in out["needs_review"]:
                print(f"REVIEW  {r['name']!r} could be: {', '.join(r['candidates'])}")
        else:
            _print(add_override(conn, args.j_cmd, args.name_a, args.name_b))
    elif args.cmd == "dedupe":
        from .dedupe import merge_duplicates
        _print(merge_duplicates(connect(cfg.db_path)))
    elif args.cmd == "events":
        _print(list_events(connect(cfg.db_path), dict(kv.split("=", 1) for kv in args.params)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
