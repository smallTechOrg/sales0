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

## Phase 1.4 — Only relevant posts; Skipped by AI; View post
- **The inbox shows only relevant enquiries.** Cards are AI-genuine posts or posts a human rescued. The AI's discards are not cards.
- **Skipped by AI tab.** Lists the posts the AI judged not relevant: snippet, one-line reason, poster type, author, keyword, how often and when last seen. Searchable (snippet, reason, author, and the full text while retained). Failed triages are under a "Triage failed" filter with **Retry triage**.
  - **Rescue → enquiry**: creates the enquiry in To review (the AI's verdict stays "discard"; the card shows it, so the AI's mistake is visible). Works only while the post text is still stored (14 days); afterwards the row shows "text purged" and the action is disabled (`raw_purged`).
  - **Override: accept**: rescue plus accept in one click; recorded as an override (`is_override=true`), so it teaches the AI.
  - A rescued or overridden post leaves the Skipped list at once.
- **View post.** A card shows **View post ↗** when sales0 captured the post link; otherwise "Author profile (post link unavailable) ↗ · posted ~Xh before sales0 found it".
- Tab counts: To review · Accepted · Discarded · Skipped by AI.
- The learning loop is unchanged: only human decisions on enquiries feed it, so a Rescue/Override is the strongest signal.

### Success Criteria (Phase 1.4)
- [ ] After fixture run 1 the inbox "To review" lists exactly the 4 genuine posts (P1, P5, P6, P8) and the "Skipped by AI" tab count is 3 (P2, P3, P9); `/inbox?tab=skipped` renders their snippets and reasons.
- [ ] Searching "freelance" in Skipped by AI returns P2 and P3 (the old Discarded-tab check); "zzzz" shows the no-match empty state.
- [ ] **Rescue** on P3 creates 1 enquiry in To review (counts: review 5, skipped 2), `ai_verdict` stays "discard", the card shows the Discard badge and the AI reason, and a second Rescue returns `already_rescued`.
- [ ] **Override** on P9 → the enquiry is Accepted with "Prospect · New" and "Overrode AI"; one `triage_decisions` row with `is_override=true`; the next run's triage `fewshot_count ≥ 1`.
- [ ] After `purge_expired` with the clock advanced 15 days: the P2 row has `raw_text` NULL but keeps its snippet and fingerprint; Rescue returns `raw_purged` and the buttons render disabled with "text purged"; re-finding P2 in a run still creates no row and no LLM call.
- [ ] P1's card has `a.enquiry__link[data-link-kind=post]` labelled "View post ↗" after a run whose menu copy worked, and "Author profile (post link unavailable) ↗" with "posted ~" after one whose menu was a no-op.
- [ ] Superseded: the Phase 1 criteria that expect the AI-discard posts (P2, P3, P9, P11) as cards in To review, bulk-discard them, or find them in the Discarded tab now apply to the Skipped by AI tab or to a rescued card; "To review (7)" becomes "(4)".

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
