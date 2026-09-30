"""HTTP API (stdlib only). Thin layer over queries/sync/resolver so it can move to FastAPI later."""
from __future__ import annotations

import hmac
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Config
from .db import connect, iso, utcnow
from .enrichment import ImportError_, import_external_event
from .fetcher import fetch_wsdc
from .queries import QueryError, get_event, get_series, list_events, list_sync_runs
from .registry import RegistryError, lookup_competitor, search_competitors
from .dancers import dancer_results
from .scoresheets import coverage, discover_eepro_year, index_eepro_year, lookup_for_events, search_by_name
from .resolver import resolve_event
from .sync import SyncAlreadyRunning, add_alias, run_wsdc_sync

log = logging.getLogger("wsdc_catalog.api")
MAX_BODY = 64 * 1024


class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def make_handler(cfg: Config, fetcher=None, registry_fetch=None, sheet_fetch=None):
    sheet_kw = {"fetch": sheet_fetch} if sheet_fetch else {}
    fetcher = fetcher or (lambda: fetch_wsdc(cfg.wsdc_url))
    registry_kw = {"fetch": registry_fetch} if registry_fetch else {}

    class Handler(BaseHTTPRequestHandler):
        server_version = "WSDCEventCatalog/0.1"

        # ---------------------------------------------------------- plumbing
        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, payload):
            body = json.dumps(payload, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise HttpError(413, "body_too_large", "request body too large")
            if n == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(n))
            except json.JSONDecodeError:
                raise HttpError(400, "invalid_json", "body must be JSON") from None
            if not isinstance(data, dict):
                raise HttpError(400, "invalid_json", "body must be a JSON object")
            return data

        def _auth(self, admin: bool):
            key = cfg.admin_api_key if admin else cfg.catalog_api_key
            if admin and not key:
                raise HttpError(403, "admin_disabled", "admin endpoints are disabled (ADMIN_API_KEY unset)")
            if not key:
                return  # read endpoints open only when CATALOG_API_KEY is unset (local dev)
            got = self.headers.get("Authorization", "")
            token = got[7:] if got.startswith("Bearer ") else ""
            ok = hmac.compare_digest(token, key) or (not admin and cfg.admin_api_key
                                                     and hmac.compare_digest(token, cfg.admin_api_key))
            if not ok:
                raise HttpError(401, "unauthorized", "missing or invalid bearer token")

        def _dispatch(self, method: str):
            url = urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            params = {k: v[-1] for k, v in parse_qs(url.query).items()}
            conn = connect(cfg.db_path)
            try:
                for m, pattern, admin, fn in ROUTES:
                    if m != method:
                        continue
                    match = re.fullmatch(pattern, path)
                    if match:
                        if admin is not None:
                            self._auth(admin)
                        return self._send(*fn(self, conn, params, *match.groups()))
                raise HttpError(404, "not_found", f"no route for {method} {path}")
            except HttpError as e:
                self._send(e.status, {"error": {"code": e.code, "message": e.message, **e.extra}})
            except RegistryError as e:
                self._send(502, {"error": {"code": "registry_unavailable", "message": str(e)}})
            except (QueryError, ValueError) as e:
                self._send(400, {"error": {"code": "invalid_parameter", "message": str(e)}})
            except Exception:  # noqa: BLE001
                log.exception("unhandled error")
                self._send(500, {"error": {"code": "internal_error", "message": "internal error"}})
            finally:
                conn.close()

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        # ---------------------------------------------------------- routes
        def health(self, conn, params):
            last = conn.execute("SELECT id, status, completed_at FROM sync_runs WHERE status='success' "
                                "ORDER BY id DESC LIMIT 1").fetchone()
            return 200, {"ok": True, "time": iso(utcnow()), "last_successful_sync": dict(last) if last else None}

        def events(self, conn, params):
            return 200, list_events(conn, params)

        def event(self, conn, params, event_id):
            ev = get_event(conn, event_id)
            if not ev:
                raise HttpError(404, "event_not_found", f"no event {event_id}")
            return 200, ev

        def series(self, conn, params, series_id):
            s = get_series(conn, series_id)
            if not s:
                raise HttpError(404, "series_not_found", f"no series {series_id}")
            return 200, s

        def resolve(self, conn, params):
            body = self._body()
            meta = {k: body.get(k) for k in ("name", "start_date", "end_date", "year", "city", "country")}
            return 200, resolve_event(conn, event_id=body.get("event_id"), metadata=meta)

        def sync(self, conn, params):
            body = self._body()
            try:
                run = run_wsdc_sync(conn, fetcher, force=bool(body.get("force")),
                                    snapshot_dir=cfg.snapshot_dir, source_url=cfg.wsdc_url)
            except SyncAlreadyRunning as e:
                raise HttpError(409, "sync_in_progress", str(e)) from None
            return (200 if run["status"] == "success" else 502), run

        def sync_runs(self, conn, params):
            try:
                limit = int(params.get("limit") or 20)
            except ValueError:
                raise QueryError("limit must be an integer") from None
            return 200, {"items": list_sync_runs(conn, limit)}

        def alias(self, conn, params, series_id):
            body = self._body()
            if not body.get("alias"):
                raise HttpError(400, "invalid_parameter", "alias is required")
            if not conn.execute("SELECT 1 FROM event_series WHERE id=?", (series_id,)).fetchone():
                raise HttpError(404, "series_not_found", f"no series {series_id}")
            created = add_alias(conn, series_id, body["alias"], body.get("source") or "manual", iso(utcnow()))
            return (201 if created else 200), {"created": created, "series": get_series(conn, series_id)}

        def competitor_search(self, conn, params):
            return 200, search_competitors(conn, params.get("q", ""), **registry_kw)

        def competitor(self, conn, params, wsdc_id):
            out = lookup_competitor(conn, wsdc_id, **registry_kw)
            if out is None:
                raise HttpError(404, "competitor_not_found", f"no WSDC registry record for {wsdc_id}")
            return 200, out

        def scoresheet_search(self, conn, params):
            return 200, search_by_name(conn, params.get("name", ""), year=params.get("year"),
                                       event_id=params.get("event_id"))

        def competitor_scoresheets(self, conn, params, wsdc_id):
            comp = lookup_competitor(conn, wsdc_id, **registry_kw)
            if comp is None:
                raise HttpError(404, "competitor_not_found", f"no WSDC registry record for {wsdc_id}")
            name = f"{comp['first_name']} {comp['last_name']}"
            return 200, {"wsdc_id": comp["wsdc_id"], **search_by_name(conn, name, year=params.get("year"))}

        def scoresheet_coverage(self, conn, params):
            return 200, coverage(conn)

        def scoresheet_lookup(self, conn, params):
            body = self._body()
            ids = body.get("event_ids")
            if not isinstance(ids, list):
                raise HttpError(400, "invalid_parameter", "event_ids must be a list of catalog event ids")
            return 200, lookup_for_events(conn, body.get("name") or "", ids,
                                          **({"fetch": sheet_fetch, "delay": 0} if sheet_fetch else {}))

        def dancer(self, conn, params):
            inc = str(params.get("include_registry", "true")).lower() not in ("0", "false", "no")
            return 200, dancer_results(conn, params.get("name", ""), wsdc_id=params.get("wsdc_id") or None,
                                       year=int(params["year"]) if params.get("year") else None,
                                       include_registry=inc, **({"registry_fetch": registry_fetch}
                                                                if registry_fetch else {}))

        def scoresheet_index(self, conn, params):
            body = self._body()
            try:
                years = [int(y) for y in (body.get("years") or [body.get("year")])]
            except (TypeError, ValueError):
                raise HttpError(400, "invalid_parameter", "year (or years) is required") from None

            # Runs while the request is open: an active request keeps the Sprite awake, whereas
            # background work after the response can be paused when the Sprite goes idle.
            kw = {"fetch": sheet_fetch, "delay": 0} if sheet_fetch else {}
            try:
                runs = [index_eepro_year(conn, y, **kw) for y in years]
            except RuntimeError as e:
                raise HttpError(409, "index_in_progress", str(e)) from None
            return 200, {"runs": runs}

        def scoresheet_websites(self, conn, params):
            from .websites import discover_from_websites
            body = self._body()
            kw = {"fetch": sheet_fetch, "delay": 0} if sheet_fetch else {}
            return 200, discover_from_websites(conn, limit=int(body.get("limit") or 25),
                                               event_ids=body.get("event_ids"), **kw)

        def scoresheet_scoring_dance(self, conn, params):
            from .scoresheets import index_provider_events
            from .websites import add_scoring_dance_event
            body = self._body()
            numbers = [str(n) for n in (body.get("numbers") or []) if str(n).isdigit()]
            if not numbers:
                raise HttpError(400, "invalid_parameter", "numbers must be a list of scoring.dance event numbers")
            kw = {"fetch": sheet_fetch} if sheet_fetch else {}
            regs = [add_scoring_dance_event(conn, n, **kw) for n in numbers]
            idx = index_provider_events(conn, provider="scoring_dance", **({**kw, "delay": 0} if sheet_fetch else {}))
            return 200, {"registered": regs, "indexing": idx}

        def judges(self, conn, params):
            from .judges import list_judges
            return 200, list_judges(conn, review_only=str(params.get("review", "")).lower() in ("1", "true"))

        def judge_override(self, conn, params, action):
            from .judges import add_override
            body = self._body()
            names = body.get("names") or []
            if len(names) != 2:
                raise HttpError(400, "invalid_parameter", "names must be a list of exactly two judge names")
            return 200, add_override(conn, action, names[0], names[1])

        def dedupe(self, conn, params):
            from .dedupe import merge_duplicates
            return 200, merge_duplicates(conn)

        def judges_rebuild(self, conn, params):
            from .judges import rebuild
            return 200, rebuild(conn)

        def scoresheet_scoring_dance_scan(self, conn, params):
            from .websites import scan_scoring_dance
            body = self._body()
            kw = {"fetch": sheet_fetch, "delay": 0} if sheet_fetch else {}
            rep = scan_scoring_dance(conn, start=body.get("from"), end=body.get("to"), **kw)
            rep["events"] = rep["events"][-50:]
            return 200, rep

        def scoresheet_discover(self, conn, params):
            body = self._body()
            try:
                years = [int(y) for y in (body.get("years") or [body.get("year")])]
            except (TypeError, ValueError):
                raise HttpError(400, "invalid_parameter", "year (or years) is required") from None
            return 200, {"runs": [discover_eepro_year(conn, y, **sheet_kw) for y in years]}

        def import_event(self, conn, params):
            try:
                return 200, import_external_event(conn, self._body())
            except ImportError_ as e:
                raise HttpError(400, "invalid_parameter", str(e)) from None

    # (method, path regex, admin? None=public False=read-key True=admin-key, handler)
    ROUTES = [
        ("GET", r"/health", None, Handler.health),
        ("GET", r"/events", False, Handler.events),
        ("GET", r"/events/([A-Za-z0-9_]+)", False, Handler.event),
        ("GET", r"/series/([A-Za-z0-9_]+)", False, Handler.series),
        ("POST", r"/events/resolve", False, Handler.resolve),
        ("GET", r"/competitors/search", False, Handler.competitor_search),
        ("GET", r"/competitors/(\d+)", False, Handler.competitor),
        ("GET", r"/competitors/(\d+)/scoresheets", False, Handler.competitor_scoresheets),
        ("GET", r"/scoresheets/search", False, Handler.scoresheet_search),
        ("GET", r"/scoresheets/coverage", False, Handler.scoresheet_coverage),
        ("POST", r"/scoresheets/lookup", False, Handler.scoresheet_lookup),
        ("GET", r"/dancers/results", False, Handler.dancer),
        ("POST", r"/admin/scoresheets/index", True, Handler.scoresheet_index),
        ("GET", r"/judges", False, Handler.judges),
        ("POST", r"/admin/judges/(merge|separate)", True, Handler.judge_override),
        ("POST", r"/admin/judges/rebuild", True, Handler.judges_rebuild),
        ("POST", r"/admin/events/dedupe", True, Handler.dedupe),
        ("POST", r"/admin/scoresheets/websites", True, Handler.scoresheet_websites),
        ("POST", r"/admin/scoresheets/scoring-dance", True, Handler.scoresheet_scoring_dance),
        ("POST", r"/admin/scoresheets/scoring-dance/scan", True, Handler.scoresheet_scoring_dance_scan),
        ("POST", r"/admin/scoresheets/discover", True, Handler.scoresheet_discover),
        ("POST", r"/admin/sync-events", True, Handler.sync),
        ("GET", r"/admin/sync-runs", True, Handler.sync_runs),
        ("POST", r"/admin/series/([A-Za-z0-9_]+)/aliases", True, Handler.alias),
        ("POST", r"/admin/events/import", True, Handler.import_event),
    ]
    return Handler


def serve(cfg: Config, host: str = "0.0.0.0", port: int = 8080, fetcher=None,
          registry_fetch=None, sheet_fetch=None) -> ThreadingHTTPServer:
    connect(cfg.db_path).close()  # create schema up front
    return ThreadingHTTPServer((host, port), make_handler(cfg, fetcher, registry_fetch, sheet_fetch))
