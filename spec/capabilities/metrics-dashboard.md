# Capability: Metrics Dashboard (Weekly + Secondary sheets)
**Phase 4**

## What It Does
Automatically produces the founder's Weekly metrics per ISO week, plus the Secondary per-source metrics against weekly targets, funnel conversion, and keyword/group yield.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Enquiries, decisions, prospects, leads, timeline, outreach | records | All modules | Yes |
| Weekly targets | numbers (editable) | Seeded: LinkedIn scraping 15, WhatsApp scraping 5, LinkedIn outreach 5, messages sent 25, first follow-ups 20 | Yes |
| Manual counts (blogs/posts published, people added to WA group, old prospects looked up) | numbers per week | User | No |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Weekly table per ISO week: enquiries added, enquiries contacted, meetings/proposals with pricing, old prospects looked up, blogs/posts published, people added to WA group | table | Metrics page, CSV export |
| Secondary: per-source enquiries added vs target, messages sent, first follow-ups vs target | table with progress bars | Metrics page |
| Funnel (enquiry → accepted → contacted → lead → won) and keyword/group yield (accepted ÷ seen) | charts/tables | Metrics page |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | SQL over the full data (no sampling, no LLM) | — |

## Business Rules
- Weeks are ISO weeks in the configured timezone.
- Each metric is computed from the full data, never from a sample.

## Success Criteria
- [ ] On a seeded fixture of ≥300 enquiries over 6 ISO weeks with known counts, every weekly cell equals the precomputed value.
- [ ] The keyword yield for a keyword with 10 seen / 3 accepted shows 30%.

## Phase 4 build (as implemented)
Page `/metrics` (replaces the stub): Weekly, Secondary, Funnel, Keyword yield, Group yield, Cost per relevant enquiry; every table has a CSV export (`/metrics/export/<weekly|secondary|funnel|keywords|groups|cost>.csv`, same `weeks`/`mode`/`week` query). Charts are server-rendered inline SVG (no JavaScript, works offline) with an accessible table under each.

### Weekly rows (computed from the CRM, ISO weeks Monday 00:00 to Sunday 23:59:59 in the configured timezone)
| Sheet row | Computed as |
|---|---|
| Number of Enquiries added | relevant enquiries created that week: `origin` discovery or manual, `ai_verdict = genuine` (manual: not discarded). Sheet/import origins are never counted (their `date_added` is the sync time). |
| Number of Enquiries contacted | prospects whose first real (non-estimated) `contacted_at` falls in the week |
| Number of Meetings or Proposals with pricing | distinct leads with a `lead_created` (meeting/proposal stage), `lead_stage` to meeting_scheduled/proposal_sent or `proposal_sent` timeline event that week |
| No. of old prospects lookup | Assumed mapping: distinct prospects created BEFORE the week that have a `research` or `tracked_changed` timeline event in it |
| No. of blogs or social posts published, No. of people added to the whatsapp group | manual entry per ISO week (`manual_metrics`), the app cannot observe them |

### Merge with the sheet baseline (never summed, so nothing is double counted)
Each cell is ONE value with a source tag (`computed` / `sheet` / `manual`; "-" = no data). Toggle `mode`: `max` (default: where app and sheet both have a figure the greater wins, tie goes to the app, because the app only sees work done in the app while the team may still log in the sheet), `computed` (app wins, sheet fills weeks the app has no data for) or `sheet` (sheet wins, app fills gaps). Weeks before the app's first recorded activity (first seen post, scrape run, own enquiry or outreach message; sheet-sync rows do not count) have no computed figure. Manual rows: a manual entry beats the sheet except in `sheet` mode. The sheet has no year: the latest repeat of a week number is the nearest past ISO year, earlier repeats step back one year each.

### Secondary
Sources: LinkedIn keyword scraping (relevant discovery enquiries), LinkedIn profile watch (`tracked_changed` events), Manual / paste (manual-origin enquiries), WhatsApp (shown "not enabled"), LinkedIn outreach (contact records with channel `linkedin*`, distinct prospects), Messages sent (sent outreach emails + manual contact records whose channel is not email), First follow-ups (distinct prospects moved to Follow-up 1). Targets: user override (`app_meta` key `metrics_target.<source>`) > the sheet's Secondary tab (`sheet_metrics.weekly_target`) > built-in default (LinkedIn scraping 15, WhatsApp 5, LinkedIn outreach 5, messages sent 25, first follow-up 20). Progress bar = actual / target; week-over-week delta against the previous ISO week.

### Funnel, yield, cost
Funnel: scanned (seen posts) -> relevant -> accepted prospect -> contacted -> replied / lead -> proposal -> won / client. After "relevant" it is a cohort by the week the enquiry was found; each enquiry is counted once and a later stage implies the earlier ones. Keyword yield reuses `services/keyword_yield` (scanned, relevant, relevant %) plus prospects created and AI cost (triage calls on that keyword's seen posts) and cost per relevant enquiry; "prune candidate" = active keyword, >= 30 posts scanned over >= 2 runs, zero relevant. Groups are listed with "not enabled" (no scan data). Cost per relevant enquiry = all `llm_calls` cost in the window / relevant enquiries, weekly and overall.

### Digest
Section `weekly_pulse` ("Weekly pulse", order 28) lists each source's figure vs its target for the ISO week containing the digest window's end.

### Performance
Constant number of indexed queries (rows bucketed into weeks in Python; new indexes on `seen_posts.first_seen_at`, `timeline_events(kind, occurred_at)`, `outreach_messages.sent_at`). Seeded test: 340 relevant enquiries, 2090 seen posts, 7 ISO weeks, every weekly figure asserted exactly.
