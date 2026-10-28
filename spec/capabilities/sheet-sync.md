# Capability: Google Sheet Sync (read-only engine)
**Phase 2 (engine built ahead of the CRM tables; the CRM-backed repository is Phase 2 slice S2)**

## What It Does
One-way, READ-ONLY sync from the team's Google Sheets into the CRM. The sheet is the source of truth for the fields it holds; the app never writes to Sheets. The engine is self-contained (`src/sales0/services/sheetsync/`): it parses each tab into canonical records, diffs them by row hash against a `SheetRepository`, and reports counts. The CRM-backed repository (`SqlSheetRepository`) is described under "Phase 2: the CRM repository".

## Sheets (defaults, overridable by settings)
| Sheet | id | Tabs (gid) |
|---|---|---|
| Client Sourcing | `1xUdfMX...Ag` (`SALES0_SHEET_CLIENT_SOURCING_ID`) | Active Enquiries 0, Discarded Enquiries 1948969217, Outreach 219858330, Prospects 1516649000, Working Sheet 490067020, Competitors 945694185, Groups 194345607 |
| Metrics | `1tnwiB...Aw` (`SALES0_SHEET_METRICS_ID`) | Weekly 1915564504, Secondary 569887510 |
`SALES0_SHEET_TABS_JSON` replaces the tab list (`[{"name","kind","gid","sheet_id"?}]`, kind in enquiries, discarded, outreach, prospects, working, competitors, groups, weekly, secondary). Fetch: `GET https://docs.google.com/spreadsheets/d/<id>/export?format=csv&gid=<gid>` (public, no auth).

## Read-only guarantee
`ReadOnlyTransport` wraps the only HTTP client: any non-GET method, any non-https scheme, any GET with a body, and any host outside `docs.google.com`, `sheets.googleapis.com` and the per-request export download host (`doc-*-sheets.googleusercontent.com`, reached by the export's 307) raises `SheetWriteBlocked` before I/O, including on redirects. The package holds no credentials or token flow. Tests prove every verb is blocked and scan the source for write/auth primitives.

## Parsing (per tab -> frozen dataclass; natural key in brackets)
| Tab | Record | Key | Entity |
|---|---|---|---|
| Active Enquiries | `SheetEnquiry` | LinkedIn activity id / normalised post URL, else sha(emails + summary), else row hash | enquiry |
| Discarded Enquiries | `SheetDiscarded` (+ `discard_reason` = Notes) | same | discarded |
| Outreach | `SheetOutreachPerson` | LinkedIn slug, else name+company hash | prospect |
| Prospects, Working Sheet | `SheetProspect` | same | prospect |
| Competitors | `SheetCompetitor` | normalised website, else name | competitor |
| Groups | `SheetGroup` | normalised link, else name | group |
| Weekly, Secondary | `SheetMetricRow` (+ `SheetMetricWeek`) | set + group + metric (+ `#n` for repeats) | metric |

Quirks handled: trailing-space / mixed-case / reordered headers; blank header column (Outreach/Prospects spare link, Secondary metric column); typo header "Comapany"; dates with no year ("09 July": nearest past occurrence; Date Posted capped at Date Added; day-first numerics; leap day); `-`, `Not provided`, `N/A` -> null; several e-mails in one cell (prose around them ignored, `email_note` when none valid); phones normalised to `+<digits>` (10-digit/0-/91-prefixed = India; several per cell; a profile URL typed into the phone cell -> `contact_link`); bold-wrapped summaries; "Company - Topic - City | Mode" split best-effort; mojibake repaired and emoji kept; cells capped at 20 000 chars (warning); fully blank rows and Status/Priority-only placeholder rows counted as blank; ragged rows padded; duplicate "Week 43" columns kept as occurrence 1 and 2; Secondary section labels carried down; numbers like `1,200`, `10%`, `₹5,000` parsed.
Row problems never abort: `errors` (row skipped) and `warnings` (row kept, field null) are collected per tab as `(row, code)`; messages never contain cell content. A duplicate natural key within a tab re-keys the later row by row hash (warning); an identical row is an error.

## Diff / apply
Per tab: fetch -> parse -> for each record compare `row_hash` with the stored `SheetSource` for that tab: missing = **added**, same hash (and not removed) = **unchanged** (only the row number is refreshed), else **updated** (sheet-owned fields only). Rows that vanished from a tab are **soft-removed**. `--dry-run` runs the identical classification without calling `upsert`/`mark_removed`. Tabs are independent: a fetch failure or unreadable header sets `fetch_error` on that tab, syncs the others and removes nothing from the failed tab. A **mass-removal guard** refuses to soft-remove (and reports `removal_blocked` plus a `removal-blocked` error entry) when the fetched tab has a header but no usable data rows while live rows exist, or when more than half of >= 3 live rows vanish in one run. A **header sanity check** per kind (`header-mismatch`: e.g. an Active-Enquiries header served for a person tab, or a person header for Competitors) makes the tab an error and nothing is ingested or removed. Unknown sheet statuses are counted per tab (`unknown_statuses`), never errors; a Lead exists only after contact AND a response (see `data.md` status tables). `mark_removed` failures surface as `remove-failed:<Name>` error entries. A re-import of an unchanged CSV writes no timeline events.

### Ownership contract (documented on `SheetRepository`)
Sheet-owned = exactly `record.sheet_fields()` (sheet sync overwrites; sheet wins). CRM-owned (timeline, AI verdicts/fields, app notes, flags, stage/owner, tags, follow-ups) is never touched. `sheet_status` / `sheet_notes` keep the raw sheet Status/Notes beside the CRM's own. An entity fed by several tabs keeps one source per `(sheet_id, gid)`; its sheet fields are the overlay of the tabs (sorted by `(sheet_id, gid)`), later tabs winning only where non-null, and it is removed only when its last live source disappears. Every create/update/remove/restore writes a timeline event with actor `sheet-sync`.

## Scheduler and bookkeeping
`SheetSyncRunner(job, interval_min=SALES0_SHEET_SYNC_MINUTES(30; 0 disables), jitter, now, executor, rng, state)`: `tick()` starts a run when due and none is active; `trigger_now()` ("Sync now") works even when disabled and returns False if a run is active; errors are caught and counted; `status()` exposes last start/finish, ok, error, per-tab report, next run, consecutive failures. First run ~20 s after construction (or last finish + interval after a restart, via the state store). `start()` runs `tick()` on a daemon thread. Table `sheet_sync_state` (singleton id 1: last_started_at, last_finished_at, last_status ok/partial/failed, last_error, last_dry_run, last_duration_s, last_report_json, runs_total), migration `0003_sheet_sync_state`, `SqlStateStore`.

## CLI
`uv run python -m sales0.cli sync-sheets [--dry-run] [--tab NAME]` - prints per-tab `rows / added / updated / unchanged / removed / errors / warnings` only. Exit 1 if any tab failed to fetch, 2 for an unknown tab. Uses the registered CRM repository (`register_repository_factory`), else an in-memory one (nothing persisted).

## Phase 2: the CRM repository (S2) — what is added on top of the engine
The engine is unchanged except that it collects `repo.drain_errors()` after each applied row. New:
- **`SqlSheetRepository`** (`services/sheetsync/sql_repository.py`) implements `SheetRepository` on the Phase 2 tables. State: table `sheet_sources` (one row per `(entity, key, sheet_id, gid)` with `row_number`, `row_hash`, the tab's `fields` snapshot) plus the columns `sheet_status`, `sheet_notes`, `sheet_removed_at`, `sheet_fields` on `enquiries`, `prospects`, `competitors`, `channel_groups` (`data.md` → Sheet-fed tables). One short transaction per record; a failing record rolls back alone, is reported as `apply-failed:<ExceptionName>` on its row and keeps its OLD hash so the next sync retries it.
- **Mapping** (`services/sheetsync/mapping.py` + `status_map.py`): the exact record→column table, the Active/Outreach/Discarded status→stage tables and the "adopt, never duplicate" rules are in `data.md` → "Sheet status → CRM stage mapping" and "Sheet record → CRM column mapping". Summary: Active Enquiries → accepted `enquiries` (+ company, contacts, a prospect, and a lead/client when the status or dates show a response); Discarded Enquiries → discarded `enquiries` with the reason (no learning-loop decisions); Outreach / Prospects / Working Sheet → `prospects` keyed by LinkedIn slug (the profile's existing prospect is adopted), tagged by source tab; Competitors → `competitors` (seeded rows adopted); Groups → `channel_groups`; Weekly / Secondary → `sheet_metrics` + `sheet_metric_weeks`.
- **Timeline:** every create / update / remove / restore writes an event with actor `sheet-sync` (field names only). Stage changes the sync applies are `stage_change` events with the same actor. No reminders are created by the sync.
- **Surfaces:** `sync-sheets [--dry-run] [--tab NAME] [--memory]` and `crm-counts` (CLI); scheduler job `sheet_sync` becomes ready (still opt-in on the Schedule page; the interval runner is `SALES0_SHEET_SYNC_MINUTES`, default 30, first run ~20 s after boot, never overlapping, state in `sheet_sync_state`); the **Sync** page (`/sync`: Sync now, last sync, per-tab added / updated / unchanged / removed / errors / warnings, last error). A dry run never writes (not even `sheet_sync_state`).
- **No row lost without an error entry:** per tab, `rows == added + updated + unchanged + distinct error rows`; asserted for every fixture tab.
- **Read-only guarantee unchanged and extended:** the guard test also scans the new modules; nothing in `sales0` can issue a non-GET request to Google.

## Assumed
- Sheet Status -> CRM stage mapping: decided in Phase 2 and recorded in `data.md` (tables) and `NOTES.md` → Phase 2 spec; a stage a human set in the app is never overwritten by the sync.
- Row-hash fallback keys are unstable under edits (edit = remove + add); accepted for rows with no URL, e-mail or summary.
- If a duplicate natural key disappears from the sheet, the surviving row may change key (documented risk).
- The CSV export redirects to `doc-*-sheets.googleusercontent.com`; that GET-only host is allowed in addition to the two named hosts.
- "Date Posted"/"Date Added" years are inferred once and frozen by the unchanged row hash.

## Success Criteria
- [x] Sync twice = 0 changes; a sheet edit updates only sheet-owned fields; a deleted row is soft-removed and restored on return.
- [x] The same person in Outreach and Prospects is one `prospect` with two sources.
- [x] No write is possible (transport guard + source scan tests).
- [x] Live read-only dry-run on both real sheets: Active Enquiries > 0 rows, 0 parse errors.
