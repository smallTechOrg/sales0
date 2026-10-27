# Capability: Review Inbox (Accept / Discard / Override, Bulk, Search)
**Phase 1**

## What It Does
Shows triaged enquiries as cards, so the team can accept genuine buyers (they become Prospect · New), discard the rest with a reason (kept, searchable, feeding the learning loop), or override the AI's verdict, priority or offering. Each action works one card at a time or in bulk.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Tab | to review / accepted / discarded | User | Default "to review" |
| Search text | string | User | No |
| Decision | accept / discard (+ discard reason) | User, per card or bulk | Yes for a decision |
| Priority / offering change | select | User | No |
| Retry triage | action | User (on a failed card) | No |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Cards (see `ui.md`) | HTML | Inbox |
| Decisions | records (decision, AI verdict at the time, override flag, reason, time, actor) | Learning loop; Phase 2 CRM |
| Status history | records | Enquiry history (Phase 2 timeline) |
| Tab counts | numbers | Tabs |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| AI triage (Retry only) | one call | Card stays failed with the new error, shown in an error toast |

## Business Rules
- The "To review" tab shows pending-review enquiries: failed triage first, then AI-genuine, then by priority, then newest. Pending (not yet triaged) enquiries show "AI triage pending…" and cannot be decided until triaged or failed. Failed ones CAN be decided by hand.
- **Accept** sets the review status to accepted and the stage to Prospect · New, and records a decision. If the AI said discard, the decision is an override.
- **Discard** sets the review status to discarded and records the reason: a chosen preset, or the AI's reason if none is chosen. If the AI said genuine, the decision is an override.
- Changing the priority or offering on a card stores the new value and records which field changed from what. The AI's original value is kept.
- An enquiry can be re-decided from the Accepted or Discarded tab ("Discard instead" / "Accept instead"). Every decision is appended to the history, and the latest decision wins.
- Bulk accept/discard applies to the selected cards. One discard reason applies to all of them. The tab counts update without a full reload.
- Search is a case-insensitive substring match over raw text, summary, company and author name, within the current tab.
- Discarded enquiries are never deleted.
- Every decision writes a status-history row with the from/to values and the time.

## Success Criteria
- [ ] After fixture run 1, "To review (7)" lists P1 before every discard-verdict card, and the P1 card shows "Genuine", its reason, its summary containing "Acme", "Open post" linking to `{base}/feed/update/urn:li:activity:7300000000000000001/`, and a cost text matching `AI \$0\.\d{4}`.
- [ ] Accepting P1 moves it to the Accepted tab with "Prospect · New". The counts become review 6 / accepted 1, and there is 1 `triage_decisions` row with `is_override=false`.
- [ ] Bulk-discarding P2 + P3 with reason "Looking for individual trainers" moves both to Discarded with that reason. Discarded count = 2.
- [ ] Accepting P9 (AI: discard) shows the "Overrode AI" badge and stores `is_override=true`.
- [ ] In Discarded, searching "freelance" returns P2 and P3, and searching "zzzz" shows the no-match empty state.
- [ ] Changing P1's priority to Medium persists across a reload, `ai_priority` stays "high", and `field_overrides` records `{"priority": ["high","medium"]}`.
- [ ] "Discard instead" on an accepted card moves it to Discarded and leaves 2 decision rows for that enquiry.
- [ ] After a server restart, the accepted and discarded cards are still in their tabs (state survival).
- [ ] Empty DB: the inbox shows "No enquiries yet…" with a Setup link.
- [ ] A decision on a deleted or nonexistent id returns the `not_found` toast and the page does not break.
