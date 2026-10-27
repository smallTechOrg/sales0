# Capability: Sheet Import + Manual Paste
**Phase 2**

## What It Does
Imports the existing Google Sheets: Active Enquiries, Discarded Enquiries, Outreach/Prospects, Competitors and Groups, exported as CSV. It also lets a user paste a single enquiry by hand. Pasted enquiries go through the same dedupe and AI triage.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| CSV file + sheet kind | upload | User | Yes (import) |
| Pasted text (+ optional link, author) | text | User | Yes (paste) |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Import batch summary (total, new, merged, errors with row numbers) | page | Import page |
| Enquiries, discarded enquiries, prospects, competitors, groups | records | Their modules |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| AI triage | pasted enquiries only. Imported rows keep the team's historical decision and are NOT re-triaged. | Same as triage |

## Business Rules
- Column mapping follows `data.md` → Sheet → field mapping. Headers are matched case- and space-insensitively. Unknown columns are ignored and reported.
- Imported Active rows become accepted enquiries plus prospects. Discarded rows become discarded enquiries plus `triage_decisions` (decision discard, reason). This seeds the learning loop with history.
- Dedupe uses the same content hash as discovery, so re-importing a file is idempotent.
- Dates are parsed in DD/MM/YYYY first (Indian sheets), then ISO.

## Success Criteria
- [ ] Importing a 120-row Active fixture CSV then re-importing it gives new 120, then new 0 / merged 120.
- [ ] A Discarded CSV row with reason "they are looking for trainers" creates a discarded enquiry and a decision whose reason is that text. The next triage call has fewshot_count ≥ 1.
- [ ] A pasted post appears in To review with an AI verdict.
- [ ] A row with an unparseable date is reported with its row number, and the other rows still import.
