# WSDC Event Catalog — Specification (v2)

Extends the WCS Results Agent (the "Sprite") so it owns a persistent, authoritative
catalog of WSDC events. The Replit application reads events from this API and never
scrapes the WSDC calendar itself.

## 1. Ownership

| Sprite owns | Replit owns |
|---|---|
| Canonical event catalog (series + occurrences) | Users, competitor profiles |
| Event aliases | Event attendance selections |
| WSDC calendar synchronization | Confirmed results |
| Historical event enrichment | Analytics and UI |
| Scoring-source discovery and result retrieval | |

Replit stores only the Sprite's stable `event_id` (an occurrence id such as `evt_…`).
Ids are opaque, never reused, and never change once issued.

## 2. Source: the WSDC event list

Primary source: `https://worldsdc.com/events/` (an HTML table). Observed format as of
2026-09-24:

| Column | Example | Notes |
|---|---|---|
| Date | `Sep 24 - 28, 2026`, `Oct 28 - Nov 2, 2026`, `Dec 28 2026 - Jan 3 2027` | Three range formats |
| Event Name | `<a href="site">The After Party aka TAP</a> Registry Event` | Status label follows the link: `Registry Event`, `Trial Event`, or nothing |
| Event Location | `Wailea, Hawaii/Maui, USA`, `Liège, , Belgium`, `Hartford, CT` | Free text, inconsistent |
| Country | flag image + link `?country=USA` | ISO 3166 alpha-3; sometimes `transparent` or empty |

Observed facts the design must handle:

- The list shows only current and upcoming events. Past events drop off every day.
- The same series appears once per year (Atlanta Swing Classic 2026, 2027, 2028).
- The source contains duplicate listings (Waterloo appears twice with different flags;
  Swingside Invitational appears twice with dates one day apart).
- Hiatus is marked inside the name: `Soul Flow ... (Hiatus -- 2026)`.
- Some rows have no status label. Treat these as `event_status = unknown`; never guess.
- There is no per-event WSDC page or id on the list. `wsdc_source_url` is the list URL.

**Confirmation status.** The page offers `?hideunconfirmed=1`. The sync fetches both
views: rows in both are `confirmed`, rows only in the full list are `unconfirmed`. If
the second fetch fails, status is `unknown` for new rows and unchanged for existing rows.

> Verify on the Sprite: the parser was written from the table as rendered on
> 2026-09-24. Confirm the live HTML keeps the header texts, the `?country=` links,
> and the meaning of `hideunconfirmed` (checkpoint in §10).

Fetch politely: identifying User-Agent, 30 s timeout, 2 retries, at most one sync per
hour outside manual runs.

## 3. Data model (SQLite)

An **event series** is the recurring event ("Boogie By The Bay"). An **event** is one
dated occurrence of it ("Boogie By The Bay, Oct 8–12 2026"). Aliases attach to the
series, so they apply to every year.

### event_series
`id, canonical_name, normalized_name, source, created_at, updated_at`

- `canonical_name` is set once, from the first source that creates the series.
- It is never overwritten by another provider's naming. Renames happen only through an
  admin action; the old name is kept as an alias.

### events (occurrences)
`id, series_id, name, normalized_name, start_date, end_date, year, city, region,
country, country_raw, location_raw, event_status, confirmation_status, hiatus, active,
lifecycle_status, source, website_url, wsdc_source_url, raw_payload, content_hash,
first_seen_at, last_seen_at, last_synced_at, created_at, updated_at`

| Field | Values and meaning |
|---|---|
| `event_status` | `registry` \| `trial` \| `unknown` |
| `confirmation_status` | `confirmed` \| `unconfirmed` \| `unknown` |
| `active` | 1 when the event is on the current WSDC list |
| `lifecycle_status` | `listed` (on the list now); `past` (dropped off after its end date); `missing_from_source` (dropped off before its end date, possibly cancelled, worth a look); `historical` (added from a non-WSDC source) |
| `source` | `wsdc_calendar` \| `scoring_dance` \| `wsdc_competitor_record` \| `manual` \| other provider |
| `country` | ISO alpha-3 code |
| `raw_payload` | JSON of the raw source cells, preserved verbatim |

### event_aliases
`id, series_id, alias, normalized_alias, source, created_at`

- Unique on `(series_id, normalized_alias)`.
- Aliases of 4 characters or fewer (TAP, GPS, SNOW) are "short". They are never used
  alone to merge series, and only count in resolution with date or location
  corroboration.

### event_observations
`id, event_id, sync_run_id, source, observed_at, content_hash, raw_payload,
previous_values`

- Written only when an event is inserted or its content changes.
- This is the history that satisfies "preserve previous values".

### event_external_refs
`id, event_id, source, external_id, url, created_at`

- Links a catalog event to its record on scoring providers.

### sync_runs
`id, source, source_url, started_at, completed_at, status, records_found,
records_added, records_updated, records_unchanged, records_deactivated,
records_skipped, errors, warnings, changes, snapshot_path`

- `status`: `running` \| `success` \| `failed` \| `aborted_guard`.
- `errors`, `warnings` and `changes` are JSON.
- The raw HTML of each run is saved to disk (last 14 kept). A failed parse can be
  debugged against the exact page that was fetched.

## 4. Normalization

- **`normalize_text`**:
  1. Strip accents and lowercase.
  2. Replace `&` with `and`.
  3. Replace non-alphanumerics with spaces and collapse whitespace.
- **Event name normalization**: `normalize_text` after removing hiatus notes and 4-digit
  years ("Mooseland Swing 2026" → `mooseland swing`).
- **Canonical name derivation**: peel off `aka X`, a trailing `(X)`, and a trailing
  `"X"`; each peeled part becomes an alias. The full listed name is also an alias.
  - `The After Party aka TAP` → canonical `The After Party`; aliases `The After Party aka TAP`, `TAP`.
  - `MADjam (Mid Atlantic Dance Jam)` → `MADjam`; alias `Mid Atlantic Dance Jam`.
- **Dates**: the three range formats above plus single dates; cross-year ranges handled.
  - Rows with unparseable dates are skipped and recorded in `errors`, never guessed.
- **Location**:
  1. Split on commas and drop empty or `n/a` parts.
  2. De-duplicate repeated parts.
  3. Take the first part as city.
  4. Take trailing country names as country text.
  5. The rest is region.
  - `location_raw` is always kept.
- **Country**: text-derived code when it disagrees with the flag (the Waterloo case, with
  a warning); otherwise the flag code; `transparent` or empty flags fall back to text.
  API filters accept `USA`, `US` or `United States`.

## 5. Synchronization algorithm

1. **Lock.** Refuse to start if another run is `running` and started < 2 h ago; mark
   older `running` rows `failed` as stale.
2. **Fetch** the full list and the `hideunconfirmed` list. Save a snapshot.
3. **Parse.** Locate the table by header text, not position.
4. **Guard.** Abort (`aborted_guard`, no writes) if 0 rows parse, or if fewer than 70 %
   of the last successful run's `records_found`. A `force` flag overrides the guard. This
   stops a layout change from deactivating the whole catalog.
5. **In one transaction, for each row:**
   1. **Resolve the series.** Match on canonical normalized name, then on long aliases;
      create the series if nothing matches.
   2. **Skip in-source duplicates.** Same series, dates and country already seen this
      run → `records_skipped`, with a warning.
   3. **Match an occurrence**, never matching one occurrence twice in a run:
      - (a) exact: same series, same start date, compatible country;
      - (b) fuzzy: same series, start within 21 days, compatible country, closest wins.
        This catches date changes.
   4. **Write:**
      - no match → insert (`added`);
      - tracked fields changed, or event was inactive → update and write an observation
        with previous values (`updated`);
      - otherwise → touch `last_seen_at` (`unchanged`).
6. **Deactivate.** Every `wsdc_calendar` event not seen: `active = 0`, and
   `lifecycle_status = past` if `end_date < today`, else `missing_from_source`.
   - Nothing is ever deleted.
   - Events from other sources are never deactivated by a WSDC sync.
7. **Record** the run with counts, errors, warnings and a change log.

## 6. Historical enrichment

`POST /admin/events/import` accepts an event found on a scoring provider or a WSDC
competitor record.

- It resolves the series the same way as the sync.
- It matches an existing occurrence from any source (series + start within 7 days +
  compatible country).
  - Match: attach an `event_external_refs` row and add the provider's name as an alias
    (source = provider).
  - No match: insert a new occurrence with that `source` and
    `lifecycle_status = historical`.
- Absence from the WSDC list never implies an event didn't exist.

## 7. API

All responses are JSON. Errors use `{"error": {"code", "message"}}`.

**Read endpoints** (`Authorization: Bearer $CATALOG_API_KEY`; open only if the key is
unset, for local dev):

| Endpoint | Purpose |
|---|---|
| `GET /events` | Filters: `year`, `country`, `event_status`, `confirmation_status`, `active`, `lifecycle_status`, `series_id`, `search`, `date_from`, `date_to`, `limit` (≤ 500, default 50), `offset`. Sorted by `start_date`. Returns `{items, total, limit, offset}`. |
| `GET /events/:id` | Event plus series aliases, external refs and `raw_payload` |
| `GET /series/:id` | Series with aliases and all its occurrences |
| `POST /events/resolve` | Body `{event_id}` or `{name, start_date?, year?, city?, country?}`. Returns `status` (`resolved`, `ambiguous` or `not_found`), `confidence`, `candidates`, `match_context`. |

`search` matches normalized occurrence names, series names and aliases.

**Admin endpoints** (`Authorization: Bearer $ADMIN_API_KEY`; disabled entirely if unset):

| Endpoint | Purpose |
|---|---|
| `POST /admin/sync-events` | Body `{force?: bool}`. Returns the run summary; `409` if a run is in progress. |
| `GET /admin/sync-runs?limit=` | Sync history |
| `POST /admin/series/:id/aliases` | Body `{alias, source}` |
| `POST /admin/events/import` | See §6 |

`GET /health` is unauthenticated.

## 8. Scheduling

- Daily sync, optional (`serve --schedule`). The scheduler checks every 15 minutes
  whether 24 h have passed since the last successful run, so restarts and wake-ups
  don't double-sync or skip.
- If the Sprite host suspends idle processes, an in-process timer will not fire. In that
  case, call `POST /admin/sync-events` from an external cron instead.
- A CLI equivalent exists: `python -m wsdc_catalog sync`.

## 9. Results-agent integration

`/find-results` calls `prepare_find_results_request(conn, body)` before any provider
lookup:

- `{event_id}` → load the event. Build `match_context` from canonical name, occurrence
  name, all aliases, dates, year and location. Replit sends nothing else.
- No `event_id` → resolve the metadata against the catalog:
  - `resolved` → proceed with that event;
  - `ambiguous` → return candidates to the caller instead of guessing;
  - `not_found` → proceed with the caller's metadata only, flagged `uncatalogued`.

## 10. Build order and acceptance checkpoint

1. Database
2. Parser
3. Manual sync command
4. Upsert logic
5. `GET /events`
6. `GET /events/:id`
7. Aliases
8. Scheduled sync
9. `/find-results` integration

Before enabling scheduling, run one live sync on the Sprite and record:

- records found, added and updated;
- three normalized example records;
- a spot check that three unlabelled rows really are unconfirmed on the website.

## 11. Competitor lookup (v0.2)

**Source:** the WSDC points registry, `POST https://points.worldsdc.com/lookup2020/find`
with the form field `q=<WSDC ID or name>`.

- The response format was confirmed against a live record on 2026-09-24.
- Registry event ids are stable per event series. Dates are month precision
  ("September 2026").
- **Coverage limit:** the registry lists only results that earned points. Prelims,
  semis and finals without points need score sheets (§12).

**Endpoints** (read key):

| Endpoint | Returns |
|---|---|
| `GET /competitors/search?q=<name>` | `{items: [{wsdc_id, first_name, last_name}]}` |
| `GET /competitors/:wsdc_id` | Dancer, roles and levels, plus `placements` |

Each placement has `role, style, division, division_abbr, result, points,
registry_event, catalog_event_id, catalog_link`. `catalog_link` is `matched` or `created`.

**Linking to the catalog:**

1. Find the series, first by `series_external_refs(wsdc_registry, <registry event id>)`,
   then by name.
2. Find the occurrence in that series whose start or end month equals the registry month.
3. If there is none, create a historical occurrence: `source = wsdc_registry`,
   `date_precision = month`, `start_date` = the first of the month.

Registry responses are cached for 12 h (`registry_cache`). Schema v2 adds
`events.date_precision`, `series_external_refs` and `registry_cache`; existing v1
databases migrate automatically.

## 12. Score sheets (next)

- Name-based search across all rounds needs per-provider adapters.
- The WSDC-approved providers are Danceconvention, Danceplace, Event Express Pro,
  EventManagement, Scoring.Dance, Swing Director, SwingWars, Vote4Dance and World Dance
  Registry.
- Plan: for each catalog event, discover the provider's results, fetch every round's sheet,
  and store a `scoresheet_entries` index (event, division, round, role, bib, name, marks,
  outcome, sheet URL).
- `GET /competitors/scoresheets?name=` and `?wsdc_id=` then query the index.
- Adapters will be built against real sample pages, one provider at a time.
