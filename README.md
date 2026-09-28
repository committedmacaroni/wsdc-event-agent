# WSDC Event Catalog

The Sprite's authoritative catalog of WSDC events. See `SPEC.md` for the full design.

**Dependencies:** Python 3.11+ and `beautifulsoup4` + `lxml` (`pip install -r requirements.txt`).
Everything else is standard library: SQLite storage, the HTTP server and `urllib` fetching.

## Quick start

```bash
pip install -r requirements.txt
export CATALOG_DB_PATH=data/catalog.sqlite3
export CATALOG_API_KEY=...        # Replit uses this for read endpoints
export ADMIN_API_KEY=...          # admin endpoints are disabled if unset

python -m wsdc_catalog sync                   # one live sync of worldsdc.com/events/
python -m wsdc_catalog events year=2026 country=USA
python -m wsdc_catalog runs --limit 3         # sync history
python -m wsdc_catalog serve --port 8080      # API; add --schedule for daily sync
python -m unittest discover -s tests -t .     # 42 tests
```

Replay a saved page:

```bash
python -m wsdc_catalog sync --from-file data/snapshots/run_000001_main.html \
  --confirmed-file data/snapshots/run_000001_confirmed.html
```

## Before enabling `--schedule` (acceptance checkpoint)

The parser was built from the WSDC table as rendered on 2026-09-24, and it was tested
against reconstructed HTML. It has not yet been run against the live page. On the Sprite:

1. Run `python -m wsdc_catalog sync`. Expect roughly 150 rows found, 0 errors and
   `status: success`.
2. Open `data/snapshots/run_000001_main.html` and confirm three things:
   - the table headers are `Date`, `Event Name`, `Event Location`, `Country`;
   - the country cell links to `?country=XXX`;
   - the `Registry Event` / `Trial Event` label sits in the name cell.
3. Check that unconfirmed detection works. `?hideunconfirmed=1` should return fewer rows
   than the full list. Then spot-check three `confirmation_status=unconfirmed` events
   against the website.
4. Run the sync a second time. Expect `records_added: 0`, `records_updated: 0`, all
   unchanged.

If step 2 or 3 fails, only `parser.py` needs changes. Everything downstream consumes
`ParsedEvent`.

## Competitor lookup

```bash
python -m wsdc_catalog competitor 29012          # full registry history, linked to catalog events
python -m wsdc_catalog competitor "Toni Watt"    # name search -> matching WSDC IDs
```

The same data is available over the API:

- `GET /competitors/search?q=Toni%20Watt`
- `GET /competitors/29012`

Both require the read key. The registry only lists results that earned points; score sheets
(all rounds) are the next phase.

## Score sheets (on demand)

Sheets are fetched only for events a dancer selects:

```bash
python -m wsdc_catalog scoresheets discover --year 2025 --year 2026    # past events -> catalog (event lists only)
python -m wsdc_catalog events year=2026 results_available=true          # what a dancer can pick from
python -m wsdc_catalog scoresheets lookup "Rose Landay" --event evt_... --event evt_...
python -m wsdc_catalog scoresheets parse https://eepro.com/results/swingtime2026/jjprelims.html   # parser check
```

The same flow over the API:

| Call | Key |
|---|---|
| `GET /events?year=2026&results_available=true` | read key |
| `POST /scoresheets/lookup {"name": "Rose Landay", "event_ids": ["evt_..."]}` | read key |
| `POST /admin/scoresheets/discover {"years": [2025, 2026]}` | admin key |

## Wiring into `/find-results`

```python
from wsdc_catalog.db import connect
from wsdc_catalog.resolver import prepare_find_results_request

def find_results(body):
    conn = connect(CATALOG_DB_PATH)
    prep = prepare_find_results_request(conn, body)   # body: {"event_id"} or {name, start_date, year, city, country}
    if not prep["ok"]:
        return prep["status_code"], {"error": prep["error"]}   # 404 unknown id, 409 ambiguous + candidates
    ctx = prep["match_context"]
    # ctx: canonical_name, occurrence_name, aliases, search_terms, start_date, end_date,
    #      year, city, region, country, event_id (None when catalog_status == "uncatalogued")
    return discover_results(ctx)   # existing scoring-provider logic, now driven by ctx
```

When a scoring provider confirms an event, call `POST /admin/events/import` with its
`external_id`. This links the provider record to the catalog event, and it also creates
historical events the WSDC list never showed.

## Scheduling on a Sprite

`serve --schedule` checks every 15 minutes whether 24 h have passed since the last
successful sync. If the Sprite suspends idle processes, that timer won't fire. In that
case, trigger `POST /admin/sync-events` daily from an external cron (GitHub Actions,
cron-job.org, or Replit's scheduler) with the admin bearer token.

## Layout

```
wsdc_catalog/
  normalize.py   names, aliases, dates, locations, countries
  parser.py      WSDC HTML table -> ParsedEvent
  fetcher.py     live fetch (+ hide-unconfirmed list), file replay
  sync.py        lock, guard, series/occurrence matching, upsert, deactivation
  queries.py     GET /events filtering + serialization
  resolver.py    event_id / metadata -> match_context for /find-results
  enrichment.py  historical imports from scoring providers
  api.py         HTTP routes + bearer auth
  scheduler.py   daily sync thread
tests/fixtures/make_fixtures.py   fixture generator (real rows, reconstructed markup)
```
