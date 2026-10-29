# Capability: AI-drafted follow-up emails (feedback round 2, E2)
**Phase 2/3 add-on** · data: `data.md` → `follow_ups.planned_channel`, `outreach_messages.follow_up_id` · builds on `timeline-followups.md`, `email-outreach.md`, `reply-tracking.md`

## What is automated, what stays human

| Step | Who |
|---|---|
| Plan how each follow-up will be done (WhatsApp / LinkedIn / Email / Call; default Email) | person (Log contact form, follow-up row) |
| Notice that an **Email** follow-up (Follow-up 1 / 2 / Final) is due | the app |
| **Write** the follow-up email (Haiku `claude-haiku-5-5`, forced tool use) into a `draft` message | the app, on a click (hosted) or the opt-in daily job (local) |
| Read, edit, **approve**, **send** (bound to the content hash; Gmail send) | **always a person** - the existing outbox state machine, unchanged |
| Mark the reminder done (channel `email`) and advance the stage | the existing send hook, when the Gmail send succeeded |

The standing rule is unchanged: **a human approves before EVERY send**. `services/followup_drafts.py` never imports `messages.approve` / `messages.send` (the static guard `tests/unit/outreach/test_no_autosend.py` still passes); the scheduler job only drafts.

## Planned channel
`follow_ups.planned_channel` (whatsapp | linkedin | email | call, default `email`) is set when the three follow-ups are created: in the **Log contact** form (one select per planned follow-up, next to its date) or by an email send, and can be changed on each open follow-up row (**the channel select**) at any time. It is *how it is planned*; `follow_ups.channel` stays *how it was done* (set when marked done). Only `email` ones are ever drafted. Switching a reminder away from Email rejects its pending draft.

## Where it lives (UI)
- **Outreach > Outbox**: card "Follow-ups due by email" - every due Email follow-up with *ready to draft* or the reason it is skipped, and the **Draft due follow-ups (N)** button (spinner while it works).
- **Pipeline** Due-today panel: the same button, and **Draft email** on each due row (or **Review draft** once one exists).
- **Prospect page** Follow-ups card: **Draft due follow-ups** (this prospect only) and **Draft email** per open Email follow-up, or "No email draft: <reason>".
- Local mode only, opt-in: Schedule page job **Draft due follow-up emails** (`followup_drafts`, daily at `SALES0_SCHEDULER_FOLLOWUP_DRAFTS_TIME`, **disabled by default**). It writes drafts under each thread's original sender; it never approves or sends.

## Rules (a reminder is skipped, with the reason shown, when ...)
- not Follow-up 1/2/Final of a prospect, or the planned channel is not Email, or it is not due yet (batch only; the per-row button works for any open Email follow-up);
- an **earlier** follow-up of the same prospect is still open ("Follow-up 1 comes first") - one at a time, in order, so a backlog never produces three emails at once;
- the prospect is closed / converted to a lead, or flagged **Do not follow up**;
- a **reply** (human) or a **bounce** exists for the prospect's emails, or a response was recorded - follow-ups stop once a reply arrives (`ReplyReceived` also cancels the open reminders, and `followups.cancel_open` rejects their pending drafts);
- a recipient is on the **suppression list** (unsubscribed / bounced) or there is no address;
- a draft for this reminder already exists (or its email was sent);
- the thread belongs to another team member (they draft it; drafts are owned by - and sent from the Gmail of - the original sender).

Safety net at send time: `messages.send` / `retry_send` refuse (`follow_up_stopped`) a follow-up draft whose reminder has since been cancelled or marked done.

## How the draft is written
Same drafting service (`services/outreach/drafting.generate_draft`) with a **follow-up variant**: system prompt `prompts/draft_followup_system.md`, `prompt_version = draft-followup-v1` (the first-email prompt stays `draft-v2`). Inputs: the prospect's newest usable **research** brief, the **account summary**, the last three **sent emails** of the thread (oldest first), the newest enquiry's requirement, the reminder's tone (friendly nudge / fresh angle / closing the loop). No deck, no template. At most 120 words, no invented facts, no prices (the same hard checks as first emails; a follow-up is regenerated for *hard* problems only, soft ones are shown on the draft as "needs attention").

**Same Gmail conversation**: the subject is forced to `Re: <original subject>`; at send time the original thread id is attached and the draft carries `In-Reply-To` / `References` of the newest message in the thread (read from Gmail; if that read fails the email simply goes out un-threaded). The threading headers are *not* part of the approval hash (the person approves the words). A prospect first contacted by hand (no app email) gets a fresh email to its primary contact address.

## Limits and cost
- At most `SALES0_FOLLOWUP_DRAFT_BATCH` (default 10) drafts per click / per scheduled run; the button then says how many are left - **click again**. Hosted mode has no scheduler: every draft happens inside the request, bounded by `SALES0_HOSTED_ACTION_BUDGET_S`.
- Stops at the monthly AI spend cap (no call is made) and after two consecutive AI failures; a fatal key/model error is shown as the usual "AI service refused" message.
- Cost: one Haiku call per draft, about 2.5K input + 250 output tokens, i.e. **about $0.0004 per draft** at the configured prices (`SALES0_LLM_PRICE_*`), recorded on `llm_calls` (`feature = drafting`, `prompt_version = draft-followup-v1`) and visible on the Costs page. Up to 10 drafts a click is about $0.004.
- Gmail daily send cap and the per-message approval still apply when the person sends.

## Success criteria
- [ ] A due Email follow-up becomes a `draft` in the outbox with subject `Re: <original>`, owned by the original sender, with no approval and nothing sent.
- [ ] Approving and sending it marks that exact reminder done via Email, advances the stage, and the Gmail message joins the original thread.
- [ ] Replied / unsubscribed / closed / Do-not-follow-up / non-Email prospects are skipped with a visible reason; a reply rejects pending drafts; a cancelled reminder's draft cannot be sent.
- [ ] 3 due prospects with a cap of 2: 2 drafts, "1 more due - click again"; the next click drafts the last.
- [ ] The scheduler job is disabled by default and, when run, only drafts.
