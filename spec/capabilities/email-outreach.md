# Capability: Email Outreach (Templates, Decks, AI Draft, Human Approval, Gmail Send)
**Phase 3**. Composition in `agent.md` → Capability 3.

## What It Does
Drafts a personalised email per prospect from the matched offering's template, attaches the matching deck (PDF library), and sends it from the team's Gmail / Google Workspace account only after a human approves that exact message.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Per-offering (or per-group) template | subject + body with placeholders | User (Outreach → Templates) | Yes |
| Deck PDFs mapped to an offering or group | files under `data/decks/` | User upload | No |
| Prospect + company brief + timeline | records | CRM, research | Yes |
| Approve / Edit / Reject | action | User | Yes before any send |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Draft (versioned) | record | Approval queue |
| Sent email (Gmail message/thread id) | record | Timeline, enquiry date_email_sent, Phase 5 reply tracking |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Gmail API (OAuth, one-time consent) | send message with attachment | Draft stays approved-but-unsent with the error and a Retry. Never re-sent automatically. |
| Anthropic (Haiku) | one drafting call | Draft not created; error toast |

## Business Rules
- **No send without approval of that exact version.** Editing an approved draft resets it to unapproved.
- Drafts never invent prices, are ≤180 words, and have no unfilled placeholders (if one remains, Approve is blocked).
- Sending records the timeline event, the prospect stage (New → Contacted) and date_email_sent.

## Success Criteria
- [ ] Drafting for the P1 prospect uses the "AI corporate workshops" template, mentions Acme Logistics, and has no `{{` left.
- [ ] Clicking Send on an unapproved draft is impossible (no control rendered), and the API route refuses it with `not_approved`.
- [ ] Real Gmail `drafts.create` + `drafts.delete` succeed in the gate. Send is exercised only against the test account itself (admin@smalltech.in mails itself; the message is trashed afterwards). There is no recipient setting.
