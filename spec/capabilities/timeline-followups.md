# Capability: Timeline + Follow-up Reminders
**Phase 2**

## What It Does
Keeps a full, timestamped timeline for every company and person: status changes, notes, emails, proposals and linked enquiries. It schedules follow-ups and lists the ones due today.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Note | text | User | No |
| Follow-up date + note | date | User / automatic on stage change | No |
| "Email sent" / "Proposal sent" marks | date | User (Phase 3: automatic) | No |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Timeline (newest first) | list | Company, contact and prospect pages |
| "Due today" and "Overdue" lists | list | Pipeline page header |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none | — |

## Business Rules
- Every stage change automatically writes a timeline event. The Phase 1 status history is shown on the timeline as well.
- Moving to Contacted, Follow-up 1 or Follow-up 2 proposes a next follow-up in 3 working days. It is editable.
- "Due today" uses the configured timezone. Overdue items stay listed until done.
- Marking a proposal as sent sets the enquiry's date_proposal_sent.

## Success Criteria
- [ ] Moving a prospect to `contacted` creates a timeline event and a follow-up due 3 working days later.
- [ ] A follow-up due today (with the clock frozen) appears under Due today. Marking it done removes it.
- [ ] The company timeline lists events from all of its prospects and enquiries in reverse chronological order.
