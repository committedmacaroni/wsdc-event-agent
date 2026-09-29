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

## 12. Score sheets, retrieved on demand (v0.4)

Score sheets are fetched only when a dancer asks for them, and only for the events the
dancer selected. There is no bulk download.

### User flow (Replit)

1. The dancer enters their name. Replit may also use `/competitors/search` to find their
   WSDC ID.
2. Replit lists events to pick from:
   `GET /events?year=2026&results_available=true`, plus `search` and `country` as usual.
   Every event carries `results_available`.
3. The dancer selects the events they attended, and Replit calls
   `POST /scoresheets/lookup {"name": "...", "event_ids": ["evt_...", ...]}` (at most 10 per
   call).
4. Each selected event comes back with a `status`:
   - `found`: the dancer appears on at least one sheet;
   - `not_on_sheets`: the sheets were checked and the name wasn't on them;
   - `no_results_source`: no supported provider has results for this event yet;
   - `event_not_found`: the event id is unknown.
   Found events include `divisions → rounds`, and each round has bib, partner, rank or
   place, the marks of each judge, counts, score, advanced, alternate and a `sheet_url`.

### Event discovery (no score sheets)

`POST /admin/scoresheets/discover {"years": [2025, 2026]}`, or
`scoresheets discover --year`, reads a provider's event list. For eepro this is one page per
year. The discovery step:

- adds past events to the catalog (matched to existing events, or created as historical);
- upgrades month-precision registry events to exact dates;
- stores each event's result-page links in `provider_events`.

The daily scheduler refreshes the current year's list. A lookup for an event with no known
provider also triggers discovery for that event's year, at most once per 24 h.

### Caching

- Fetched sheets are stored in `scoresheet_sheets` and `scoresheet_entries`, and reused by
  later lookups.
- For events that ended within 14 days, sheets older than 1 h are re-fetched, since results
  may still be corrected.
- Requests to the provider are spaced 0.3 s apart during a lookup.
- `GET /scoresheets/search?name=` searches only sheets already retrieved.

### Provider: Event Express Pro (eepro.com)

Formats were observed on 2026-09-24:

- **Year index:** event headers (`"September 10-13, 2026 - SwingTime"`) followed by links to
  `/results/<slug>/<page>.html`.
- **Prelim, quarter and semi sheets:**
  - Columns: Count, Competitor, one per judge, BIB, Counts (Y-A-N), Sum, Promote, Alt.
  - Rows without a Count did not advance.
- **Final sheets:** Place, Competitor ("Leader and Follower"), one per judge, BIB
  ("321/172"), Marks Sorted.

The parser uses table cells when present and whitespace tokens otherwise. Rows are parsed
right to left, and the judge count comes from each row. Couples split into leader and
follower entries. PDF result pages are listed but not parsed.

### Matching and limits

- **Catalog event to provider event:** first by the link created during discovery. If none
  exists, the fallback is a provider event starting within ±3 days whose name shares at
  least half its words with the event name, series name or an alias.
- **Dancer names:** exact match, ignoring case and accents. Dancers with the same name merge,
  and spelling variants are missed.
- **Remaining providers:** Danceconvention, Danceplace, EventManagement, Scoring.Dance,
  Swing Director, SwingWars, Vote4Dance and World Dance Registry. Each adapter supplies
  `parse_index` and `parse_sheet` in the same shapes.

## 13. Dancer results: name in, every event out (v0.5)

This supersedes the pick-events flow for Replit's search. **Replit sends a name and only
displays what comes back**; the agent does all of the searching.

### How the agent finds events

The agent can only know which events a name appears on by having read those sheets, so it
indexes them ahead of time.

**Backfill, once per year of history:**

- CLI: `python -m wsdc_catalog scoresheets index --year 2025 --year 2026`
- Runs about 1 s per sheet, and is safe to re-run: sheets already indexed are skipped.

**Daily upkeep:**

- Call `POST /admin/scoresheets/index {"year": <current year>}` with the admin key after the
  calendar sync.
- It runs while the request is open (an active request keeps the Sprite awake; background
  work can be paused when the Sprite idles), and returns the run summary.
- It only downloads new sheets, plus re-checks events that ended in the last 14 days.
- `409` means an indexing run is already in progress.

### Endpoint

`GET /dancers/results?name=<name>` (read key). Optional parameters:

| Parameter | Effect |
|---|---|
| `wsdc_id` | Picks the registry dancer when several share a name |
| `year` | Limits results to one year |
| `include_registry=false` | Skips the registry lookup |

The response:

```
{ name, total_events,
  events: [ { event_id, event_name, start_date, end_date, date_precision, city, region, country,
              sources: ["scoresheets", "wsdc_registry"],
              provider,
              divisions: [ { division, role, furthest_round, final_place,
                             rounds: [ { round, bib, partner, rank, place, marks, counts, score,
                                         advanced, alternate, competed, sheet_url } ] } ],
              registry_results: [ { role, division, division_abbr, result, points } ] } ],
  registry: { status: found | not_found | multiple | unavailable | skipped,
              wsdc_id, first_name, last_name, level_allowed, primary, secondary, candidates? },
  coverage: { providers, years: [ { year, events, sheets_indexed, last_indexed_at } ] } }
```

- **Events** are sorted newest first.
- **Score-sheet matching** is an exact name match (case and accents ignored).
- **The registry** is included when the name matches exactly one WSDC dancer, or when
  `wsdc_id` is given. It adds point-earning results at events whose sheets aren't indexed
  (for example, providers not yet supported); those events have
  `sources == ["wsdc_registry"]` and no `divisions`.
- **If the registry is unreachable**, the response still returns score-sheet results, with
  `registry.status = "unavailable"`.

## 14. scoring.dance and event-website discovery (v0.6)

### scoring.dance provider

**URLs** (every locale is normalized to `enUS`):

- Event results index: `https://scoring.dance/enUS/events/<n>/results/`
- One round: `.../results/<round>.html`

**Parsing:**

- The results index yields the event name, the date (`at MM/DD/YYYY`) and the round links.
- Live markup (captured 2026-09-29):
  - The "Bib Number" header is an image with alt text.
  - Each row carries `data-state` (`CB` = callback/advanced).
  - Each name links to the dancer's WSDC registry profile (`data-wsdc`). This is stored as
    `scoresheet_entries.wsdc_id`, and `/dancers/results` matches on it, so spelling differences
    don't matter and same-name dancers stay separate.
  - Judge headers carry the full name in `title`, which is stored instead of initials.
- Prelim, quarter and semi pages (confirmed format): the heading
  `"<Division> Jack&Jill <round> results - <Event Year>"`, then one table per role, leaders
  first then followers.
  - Columns: Bib Number, name, judge initials, Σ.
  - Marks Yes/No/Alt1-3 become Y/N/A1-A3.
- Rank is the row order (by score).
- **Finals** (confirmed format, captured 2026-09-29):
  - Columns: Bib | Leader | Follower | one placement per judge | head judge | Placement
    ("1st", an image header).
  - Each couple becomes a leader entry and a follower entry, each with their own WSDC ID and
    the other as partner. The bib goes on the leader.
  - Judges' marks are their placements. Any other table layout is reported as unrecognized
    rather than guessed.

**Registering events:**

- Automatically, from event websites (below).
- Manually, by event number: `scoresheets scoring-dance --number 195`, or
  `POST /admin/scoresheets/scoring-dance {"numbers": [195]}`.

### Event-website discovery

`scoresheets websites [--limit 25] [--event evt_...]`, or
`POST /admin/scoresheets/websites {"limit": 25}`, scans event websites for results links:

1. **Which events:** catalog events with a `website_url` that have already started and weren't
   checked in the last 7 days, most recent first. Provider sites themselves are never
   scanned as event websites.
2. **Which pages:** the home page, plus up to 3 same-site pages whose link text or path
   mentions results, scores or scoring.
3. **What gets registered:**
   - eepro links (`eepro.com/results/<slug>/`) → the eepro provider event (its year index
     is discovered if needed);
   - scoring.dance links → the event's scoring.dance results index.
4. **Year check:** the provider event attaches to the catalog event only when their start
   dates are within 10 days. Otherwise (for example, a site still linking last year's
   results) it's matched or created as its own occurrence by date.
5. **Other results links** (PDFs, the event's own pages) are stored in `website_checks` and
   returned as `website_results_links` on `GET /events/:id` and on each event in
   `/dancers/results`. They can't be searched by name.
6. **Indexing:** new scoring.dance sheets are indexed at the end of the scan.

### Other changes

- Provider-created events no longer store the provider's page as `website_url`. Migration 6
  clears values stored that way by earlier versions.
- The fetcher keeps cookies across redirects and sends `Accept-Language: en-US`, since some
  sites loop forever without cookies.

## 15. Dashboard-ready data (v0.7)

`/dancers/results` now returns the numbers Replit needs, so Replit displays them without
computing its own scoring.

**Per round** (in `events[].divisions[].rounds[]`):

- `is_final`.
- `mark_details`: one entry per judge.
  - Callback rounds: `{judge, mark: "Y"|"A1"|"A2"|"A3"|"N", label: "Yes"|"Alt 1"|..., kind: yes|alt|no, points}`.
  - Finals: `{judge, placement}`.
- Callback rounds only: `callback_points`, `callback_max` (10 × judges marking),
  `callback_pct`, and counts of `yes`, `alt` and `no`.
- Points follow the eepro sheet legend: Yes = 10, Alt 1 = 4.5, Alt 2 = 4.3, Alt 3 = 4.2,
  No = 0. The head judge's blank prelim column is excluded.

**Top level:**

| Field | Contents |
|---|---|
| `summary` | `events_competed`, `events_with_scoresheets`, `callback_rounds`, `callback_rounds_advanced`, `advancement_rate`, `avg_callback_pct`, `finals_made`, `best_final_place`, `wsdc_points`, `level_allowed` |
| `progress.callback_rounds[]` | Chart series over time for prelims, quarters and semis only: date, event, division, role, round, callback_pct, points, max, yes/alt/no, advanced, rank, competed |
| `progress.finals[]` | Kept separate: date, event, division, role, place, partner, each judge's placement |
| `judges[]` | One per judge (matched by name): events_judged, callback_marks, yes/alt/no, yes_rate, avg_points, finals_judged, avg_finals_placement, and `history[]` (every mark or placement they gave, by date) |

`callback_pct` normalizes for panel size, so rounds are comparable across events.

## 16. Judge identity resolution (v0.8)

Judge names differ across sheets and providers ("Tren" / "Trendlyon Veal", "Lisa M Picard" /
"Lisa Picard"). `judges.rebuild()` groups the variants into one person. It runs automatically
after any indexing that adds sheets, and on demand. Every decision records its method,
confidence and reason.

| Rule | Method | Confidence |
|---|---|---|
| Two names on the same panel (same sheet section) are never merged | — | hard rule |
| Same first + last name, middle name or initial differs | `middle_name` | high |
| Same last name, first names are nickname forms (built-in list) | `nickname` | high |
| Same last name, one first name is a prefix of the other (≥ 3 letters) | `first_name_prefix` | medium |
| First-name-only entry matching exactly one full-named judge | `first_name_only` | medium |
| First-name-only entry matching several full-named judges: not merged, listed with candidates | `needs_review` | low |
| Admin merge or separate (`judge_overrides`), applied before all rules | `manual` | manual |

The display name is the fullest variant (most name parts, then most panels).

**Endpoints:**

| Endpoint | Key |
|---|---|
| `GET /judges` (`?review=true` lists only names needing a decision) | read key |
| `POST /admin/judges/merge {"names": [a, b]}` | admin key |
| `POST /admin/judges/separate {"names": [a, b]}` | admin key |
| `POST /admin/judges/rebuild` | admin key |

The CLI equivalents are `judges list [--review]`, `judges merge A B`, `judges separate A B`
and `judges rebuild`.

In `/dancers/results`, `judges[]` is grouped by resolved identity. Each judge adds
`listed_as[]` (every name variant seen, with method, confidence and reason) and
`needs_review`.
