# Automatic status, one board per company, health and analytics (migration `q24_auto_status_8d2f6b41`)

## Boards: a company is only ever on ONE
* **Enquiries** - inbound enquiries nobody has reached out on yet (the company is a Prospect at New and has an accepted
  enquiry; or the enquiry has no company yet).
* **Prospects** - the outreach cadence: New (cold-list companies with no accepted enquiry), Contacted, Follow-up 1,
  Follow-up 2, Final follow-up.
* **Leads** - Meeting scheduled, Assessment, Proposal sent, Interviews, On hold (in that order). Won -> Clients;
  Lost / No response -> closed.
* **Clients** - a list.
* **Closed** companies (any off-ramp) are on no board. They stay under Companies with a Closed tag; Companies ->
  Show: Archived lists them with Reopen. Reopening returns the company to **Prospects / New** (a new prospect cycle
  when the old one became a lead; a lost client is won back as a client). The reopen is a manual status, so the
  earlier outreach does not push it forward again.

## Automatic status (`services/crm/auto_status.py`, `SALES0_AUTO_STATUS`, default on)
* Trigger: every `timeline.add_event` (notes, logged follow-ups, emails sent, replies, enquiry status changes, board
  moves, contact notes ...) records the company it touched; just before the commit `recalc` runs once per touched
  company in a savepoint (a failure is logged and never costs the action). Sheet-sync / import entries do not trigger.
* Rules (forward only): closed companies and clients are left alone; a prospect's status = the number of distinct
  days with real outreach (email sent, logged follow-up on the day it happened, follow-up marked done, logged
  contact, call): 0 New, 1 Contacted, 2 Follow-up 1, 3 Follow-up 2, 4+ Final follow-up; a logged meeting makes a
  prospect a Lead / Meeting scheduled. Lead stages are a person's call.
* Every automatic change writes a company timeline entry: "Status updated automatically: A -> B (triggered by: X)".
* Manual override: the "..." menu on the company page (behind a confirmation) or a board drag. Stored on
  `companies.status_override_*`; the automatic status holds until the next real outreach event after it, then counts
  outreach from the status the person chose. "Back to automatic status" drops it.

## Board drag
Cards drag to another column or onto the move strip (another board, Client, Close...). The drop opens the move popup:
the new status with what it means (shown while choosing), Why (optional; the close reason for a close), and the
meeting time for Meeting scheduled. Each card also has a Move... button (same popup, no drag needed).

## Replies on Cloud Run
Hosted mode has no timer, so the laptop worker asks `POST /api/worker/replies/check` every
`SALES0_WORKER_REPLY_CHECK_MINUTES` (default 10); the hosted app throttles to the same interval.

## Health (`/health`) and Analytics (`/metrics/analytics`)
* Health: replies (per member Gmail state), laptop worker, LinkedIn session, AI key and spend, runs, sheet sync,
  automatic status, database schema - each with what to do.
* Analytics: funnel (in pipeline -> contacted -> lead -> client), time in stage (median / average days), close
  reasons by month and by offering, outreach activity per week and per member, and an AI read of what worked / what
  did not (Sonnet; reads follow-up notes, drafts, sent emails, replies and notes; stored in `text_settings`).
