# Capability: CRM Pipeline (Prospects → Leads → Clients)
**Phase 2** · data: `data.md` → Phase 2 entities · API: `architecture.md` → Phase 2 (S1 contract) · screens: `ui.md` → Pipeline

## What It Does
Moves accepted enquiries and shortlisted people through the binding lifecycle: **Prospect** (New → Contacted → Follow-up 1 → Follow-up 2 → Final follow-up), then **Lead** (Meeting scheduled → Proposal sent → Won / Lost / No response), then **Client** (active, or legacy). It is driven by what the team does and by events from outreach (message sent) and reply tracking (human reply), and fed read-only by the Google Sheets.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Accepted enquiry (`EnquiryDecided`) | event | Review inbox | Yes (creates / reuses the prospect) |
| Message sent (`MessageSent`) | event | Outreach send | No (moves the prospect to Contacted) |
| Human reply (`ReplyReceived`) | event | Reply tracking | No (records the response, may open the lead) |
| Stage change, note, close, reopen, track toggle, "Response received", convert, lead stage | actions | Pipeline pages | No |
| Manual prospect, legacy client | form | User | No |
| Sheet rows | records | Sheet sync (`sheet-sync.md`) | No |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Prospects, leads, clients with stage, status and flags | records | Pipeline page, digest, Phase 4 metrics |
| Timeline events for every change | records | Timeline |
| `linkedin_profiles.tracked` | bool | Profile registry (Phase 3 watch) |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none (pure database) | — |

## Business Rules
- **One prospect per LinkedIn profile.** Accepting an enquiry is idempotent: `ensure_for_enquiry` finds the prospect of the author's `linkedin_profiles` row (unique), else creates it; a second enquiry from the same author links to the SAME prospect. Without a profile (sheet row, pasted post) there is one prospect per enquiry. It sets `enquiries.prospect_id/company_id/contact_id`, `linkedin_profiles.tracked = true` (when it was NULL), and writes timeline `created` + `enquiry_linked`.
- **Discarding** an accepted enquiry closes its prospect (`closed_reason = enquiry_discarded`) only if the prospect is still New and no other accepted enquiry is linked; accepting again reopens it.
- **Send hook:** `MessageSent` for a message that resolves to a prospect (`outreach_messages.prospect_id`, else via `enquiry_id`, else via a recipient who is a known contact) moves a New prospect to Contacted (`contacted_at` = send time), writes `email_sent`, sets `enquiries.date_email_sent` if empty and schedules the three reminders (`timeline-followups.md`). A drafted, edited, approved or failed message does nothing. A follow-up message completes the earliest open reminder.
- **Reply hook:** only `kind = human` counts as a response (auto-replies and bounces are timeline notes). Intent `interested`, `question` or `not_now` AND the prospect was contacted AND no open lead → **Lead · Meeting scheduled** (`opened_by = reply`). `unsubscribe` → flag `no_follow_up`, reminders cancelled, no lead. `other` → response recorded, a human decides.
- **Lead only after contact AND a response.** Manual conversion without `contacted_at` → `lead_needs_contact`; without `responded_at` → `lead_needs_response` (both 409 with an actionable message). At most one open lead per prospect; the prospect becomes `converted`.
- **Proposal sent** sets `leads.proposal_sent_on` and the origin enquiry's `date_proposal_sent`. **Won** creates an active client (idempotent per lead). **Lost / No response** closes the lead and sets `linkedin_profiles.tracked = false`; so does closing a prospect as `no_response`, `lost` or `not_interested`. The user can toggle `tracked` at any time.
- Moving back a stage is allowed and recorded. A human move sets `stage_source = app`; **the sheet sync never overwrites a stage once `stage_source = app`** (`data.md` mapping tables).
- Legacy clients are typed in or CSV-imported (`status = legacy`, `source = legacy_import|manual`).
- Prospects are keyed for sheet adoption by profile slug, else the sheet key; a sheet row for a profile the app already tracks adopts that prospect.

## Success Criteria
- [ ] Two accepted enquiries from one author → 1 prospect, `tracked = 1`, both enquiries have the same `prospect_id`; accepting the same enquiry twice changes nothing.
- [ ] `MessageSent` on a draft→approved→sent message (fake Gmail) → prospect `contacted`, `contacted_at` = `sent_at`, 3 reminders; the same message in state `draft` / `failed` → nothing.
- [ ] `ReplyReceived(human, interested)` on a contacted prospect → lead `meeting_scheduled`; on a `new` prospect → none; `unsubscribe` → flag + no lead; `auto_reply` and `bounce` → no lead.
- [ ] Converting a New prospect → `lead_needs_contact`; contacted without a response → `lead_needs_response`.
- [ ] meeting_scheduled → proposal_sent → won creates exactly one active client; each step has a timeline event; `lost` sets `tracked = false`.
- [ ] The migration on a DB with 3 accepted enquiries (two from one author) creates 2 prospects.

- CRM feedback (`crm-entity.md`): characteristics on the entity (prospect anchor; shown and editable on prospect / lead / client, filterable), and Inbox Accept straight to Client through `leads.promote_to_client` (lead opened without the contact/response preconditions, then Won).
