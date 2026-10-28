# Capability: Reply Tracking
**Phase 5**

## What It Does
Detects replies to emails sent from sales0, records them on the timeline, and marks the prospect as responded (which allows Lead conversion).

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Sent email thread ids | records | Email outreach | Yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Reply events + "Response received" | records | Timeline, pipeline |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Gmail API | poll threads every 15 min | Logged; retried next cycle |

## Business Rules
- Only messages in a known thread, from a non-team sender, count as replies. Auto-replies (out of office) are flagged and do not count.

## Success Criteria
- [ ] A thread with a reply from the prospect address creates a `reply` event and sets responded.
- [ ] An "Automatic reply" subject does not set responded.

## As built (Phase 5D, CRM-independent)
- `jobs/reply_poller.py` (thread, `SALES0_REPLY_POLL_MINUTES`, default 10, 0 = off, non-overlapping) ->
  `services/replies.poll_all`: per member, for SENT `outreach_messages` (<= `SALES0_REPLY_LOOKBACK_DAYS`)
  fetch the thread in THAT member's own mailbox. Read-only: no label/read/archive/send calls.
- Inbound = not from the member, their connection address, `SALES0_REPLY_OWN_ALIASES` or ANY team member;
  not SENT/DRAFT labelled.
- Classification (`services/reply_classify.py`): headers decide bounce (MAILER-DAEMON/postmaster,
  multipart/report, X-Failed-Recipients) and auto-reply (Auto-Submitted != no, X-Autoreply, Precedence
  bulk/junk/list/auto_reply, "Automatic reply" subjects, no-reply senders) with no LLM call; everything
  else goes to Haiku (feature `triage`, prompt `reply-v1`): kind human|auto_reply|bounce + intent
  interested|not_now|unsubscribe|question|other + one-line reason.
- Storage: `outreach_replies` (UNIQUE `gmail_message_id` = idempotent; snippet <= 500 chars, no body),
  `suppressions`, `reply_poll_state` (health + exponential backoff, cap 6h). Auth errors flip the member's
  connection to `needs_reauth`.
- Effects via `services/events.ReplyReceived` subscribers. Default subscriber: `reply_status`
  (replied > bounced > auto_reply), stop pending follow-ups (`follow_up_of_id`), suppress unsubscribes and
  hard bounces. Phase 2 (CRM) subscribes to the same event.
- Drafting, editing recipients, approving and sending refuse suppressed addresses (`recipient_suppressed`).
- UI: `/replies`, `/replies/suppressions`, outbox badge, header status line.
