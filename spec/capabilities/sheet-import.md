# Capability: Manual Paste + CSV Import (app-owned data)
**Phase 2** · the Google Sheets themselves are NOT imported here: they are synced, read-only, by `sheet-sync.md`.

## What It Does
Lets a user paste a single post by hand (it goes through the normal dedupe and AI triage) and import CSV files of data that does NOT come from the synced sheets: legacy clients, contacts, or a one-off export of any sheet tab. Imported rows are app-owned (`origin = import`) and fully editable.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| CSV file (≤ 5 MB, ≤ 20 000 rows) + kind | upload | User | Yes (import) |
| Pasted text (≥ 40 chars) + optional link, author, author profile URL | text | User | Yes (paste) |

Kinds: `enquiries`, `discarded`, `outreach`, `prospects`, `working`, `competitors`, `groups` (same column shapes and the same parser as the sheet tabs), plus `clients` (name, company, started_on, notes) and `contacts` (name, email, phone, linkedin, company, tags).

## Outputs
| Output | Type | Destination |
|---|---|---|
| Import batch summary (total, new, merged, unchanged, errors with row numbers and codes, never cell content) | page, `import_batches` row | Import page |
| Enquiries, prospects, contacts, competitors, groups, clients | records | Their modules; timeline `import` events |
| Pasted enquiry in To review with an AI verdict | record | Inbox |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| AI triage (Haiku) | pasted posts only; imported rows keep their given state and are NOT triaged | Paste: the enquiry is kept with `triage_status = failed` and a Retry in the inbox |

## Business Rules
- CSV rows are parsed by the SAME parser as the sheet tabs (`sheetsync.parsers.parse_tab`), so every quirk (year-less dates, several emails in a cell, "Not provided", ragged rows, duplicate headers, blank header column) behaves identically; rows are applied with the SAME mapping (`sheetsync.mapping.apply_record`, `origin = import`, actor `import`, `data.md` status table).
- **Sheet wins:** a row whose natural key already belongs to a sheet-fed record is counted `merged` and nothing is overwritten. Re-importing a file is idempotent (second run: new 0, unchanged n). Enquiry keys are the normalised post URL, else hash(email|summary), else row hash (as in sheet sync).
- A bad row (unparseable date is only a warning; no identifying content, parse failure) is reported with its row number and code and never stops the import. Unknown columns are ignored and listed.
- **Paste:** normalised and deduped with `services.dedupe` (same content hash as discovery; a duplicate answers "already in your inbox" with a link), created as `origin = manual`, `review_status = pending_review`, then ONE real triage call; accepting it is the normal inbox action (→ prospect).
- Imported discarded rows do NOT write `triage_decisions` (no learning-loop pollution; same rule as sheet sync).

## Success Criteria
- [ ] Importing a 120-row `enquiries` fixture CSV, then re-importing it: new 120 / unchanged 0, then new 0 / unchanged 120.
- [ ] A row with an unparseable date is imported with the date empty and reported (row number, code `bad-date:date_posted`); a row with no identifying content is reported as an error; the rest import.
- [ ] A competitors CSV row matching a sheet-fed competitor is counted merged and the sheet value stays.
- [ ] A `clients` CSV creates legacy clients visible under Pipeline → Clients.
- [ ] A pasted post appears in To review with an AI verdict; pasting the same text again says duplicate and creates nothing.
