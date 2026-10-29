# Capability: Timeline + Follow-up Reminders
**Phase 2** · data: `data.md` → timeline_events, follow_ups

## What It Does
Keeps a full, timestamped timeline for every prospect, lead, client, contact, company, competitor and group (status changes, notes, emails sent, replies, proposals, linked enquiries, flags, sheet changes) and schedules follow-up reminders that surface under **Due today** and **Overdue**.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Note | text (1-4000) | User | No |
| Custom follow-up (date + note), change date, mark done with channel (WhatsApp / LinkedIn / Email / Call) + note | action | User | No |
| Log contact (first contact by hand: channel, note, contact date, the three follow-up dates) | action | User | No |
| Contact observed (send hook / human move to Contacted) | event | Outreach, Pipeline | Triggers the three reminders |
| Any CRM change | internal | Services, hooks, sheet sync | Writes an event |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Timeline (newest first, 200 per page) | list | Prospect, lead, client, contact, company pages |
| Due today / Overdue panel and count badge | list | Pipeline page header, nav badge, digest |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none | — |

## Business Rules
- **Every** state change writes a `timeline_events` row with actor (member name, `system`, `sheet-sync`, `import`) and time: stage changes, notes, emails sent, replies, lead and client creation, proposal sent, enquiry links, flags set/cleared, follow-up added/done/cancelled, tracked toggled, contact merges, imports, and every sheet-driven create/update/remove/restore (body = changed field NAMES only, no cell values). Timeline rows are append-only.
- A page's timeline is the union of its scope: a prospect's page shows its own events plus those of its leads, client, enquiries and emails; a company's page shows all of its prospects' and enquiries'. The Phase 1 `status_history` of linked enquiries is merged into the read.
- **Reminders** (`Settings`: `SALES0_FOLLOWUP_DAYS_1/_2/_FINAL` = 3 / 7 / 14, calendar days from the contact date): when a prospect is observed Contacted, three open reminders are created (Follow-up 1, Follow-up 2, Final follow-up). Completing one moves the prospect to that stage and completes earlier ones; moving the prospect forward by hand completes the reminders up to that stage. A recorded response, a lead, closing, or the `no_follow_up` flag cancels the open ones (kept, with the cancel reason). Sheet-derived stages and the migration backfill create no reminders. Custom reminders can be added to prospects and leads. Completing a reminder records its channel (`follow_ups.channel` and the timeline `data.channel`); a Gmail-sent follow-up completes the earliest open one with channel `email`. Before first contact the three dates are shown as a plan in the Log contact form (editable there) and become reminders when the contact is logged.
- **Due today** = open reminders with `due_on` = today in `SALES0_TIMEZONE`; **Overdue** = earlier open ones, listed until done, snoozed or cancelled. `prospects.next_follow_up_on` mirrors the earliest open one.
- Marking a proposal as sent sets `leads.proposal_sent_on` and the origin enquiry's `date_proposal_sent`.

## Success Criteria
- [ ] Moving a prospect to Contacted with the clock frozen at D creates reminders at D+3, D+7, D+14 and a `stage_change` event; changing `SALES0_FOLLOWUP_DAYS_1` to 2 gives D+2.
- [ ] With the clock at D+3, the D+3 reminder is under Due today, D+7 is not; at D+4 the first is Overdue; marking it done removes it and moves the prospect to Follow-up 1.
- [ ] A recorded response cancels the open reminders (cancel reason stored) and the count badge drops.
- [ ] A company's timeline lists events from all of its prospects and enquiries in reverse chronological order, with Phase 1 status history merged.
- [ ] A sheet-driven update writes an event whose actor is `sheet-sync` and whose body contains field names but no cell value.
