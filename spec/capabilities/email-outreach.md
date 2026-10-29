# Capability: Email Outreach (Templates, Decks, AI Draft, Human Approval, Gmail Send)
**Phase 3**. Composition in `agent.md` → Capability 3.

## What It Does
Drafts a personalised email per prospect from the matched offering's template, attaches the matching deck (PDF library), and sends it from the team's Gmail / Google Workspace account only after a human approves that exact message.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Per-offering (or per-group) template | subject + body with placeholders | User (Outreach → Templates) | Yes |
| Deck PDFs mapped to an offering or group | `decks/<sha256>.pdf` in the attachment store (local: `<data_dir>/decks/`; hosted: GCS bucket `SALES0_ATTACHMENTS_BUCKET`), metadata in `decks` | User upload (max 10 MB) | No |
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
- **The attachment is never dropped silently.** Compose pre-selects the deck mapped to the offering (else the newest generic deck) and says why; when none applies it warns "this email will go out WITHOUT an attachment". The draft, the approval control and the sent record show the attachment (file name + size) or "No attachment"; the `approved` / `sent` audit events record it. A deck whose file is missing is flagged "file missing - upload again" and blocks approval (`deck_missing`). An edit post without a `deck_id` field keeps the deck (only an explicit `none` removes it).
- Each PDF is stored once per content; messages reference the deck by id and the approval hash covers its bytes, re-read and re-verified at send time. Hosted mode without `SALES0_ATTACHMENTS_BUCKET` refuses uploads/sends (`attachments_not_configured`) rather than using the ephemeral `/tmp`.
- **Recipient picker:** Compose lists the prospect's contacts (primary first) and its company's contacts, one option per address (all of a contact's emails); choosing one fills To and Contact name; suppressed addresses are listed disabled; free text stays for someone new; a search box finds any contact.

## Success Criteria
- [ ] Drafting for the P1 prospect uses the "AI corporate workshops" template, mentions Acme Logistics, and has no `{{` left.
- [ ] Clicking Send on an unapproved draft is impossible (no control rendered), and the API route refuses it with `not_approved`.
- [ ] Real Gmail `drafts.create` + `drafts.delete` succeed in the gate. Send is exercised only against the test account itself (admin@smalltech.in mails itself; the message is trashed afterwards). There is no recipient setting.
