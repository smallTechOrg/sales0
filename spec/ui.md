# sales0 — UI

The UI bar is `ui-ux.md`. Every view designs its empty, loading, error and populated states. Body text is 16px, contrast meets WCAG AA, every control is keyboard-reachable with a visible focus, and markup is semantic. Each value appears once on screen.

## Shell (every page)

- **Header**: the "sales0" wordmark, then the primary nav, then on the right the **status light** and the **cost badge**.
  - Status light (`#status-light`):
    - green "LinkedIn connected · last run 2h ago"
    - grey "LinkedIn not connected"
    - red "LinkedIn needs attention"
    - amber with spinner "Connecting… / Checking… / Run in progress"
    - In fixture mode it adds a `FIXTURE MODE` badge, so demo data is never mistaken for live data.
  - Cost badge (`#cost-badge`): "AI today $0.0123 · month $0.4567".
- **Nav (in order)**: Inbox · Runs · Setup (real); then Pipeline (P2) · Contacts (P2) · Competitors & Groups (P2) · Import (P2) · Outreach (P3) · Metrics (P4) · WhatsApp (P5). Stub items carry a `.badge--stub` "Phase N" next to the label.
- **Alert banner** (`#alert-banner`, `role="alert"`, under the header). It shows on every page while the session is `needs_attention`: "LinkedIn needs attention — <last_error>. Runs are paused to protect your account." It has a [Go to Setup] button and cannot be dismissed while the condition holds.
- **Toasts** region (`#toasts`, `aria-live="polite"`), bottom-right.

## Inbox (`/inbox`) — the primary screen

- **Title**: "Review inbox". Tabs (`#inbox-tabs`): **To review (n)** · **Accepted (n)** · **Discarded (n)**. A search box (`#inbox-search`, placeholder "Search text, company, author…") searches within the current tab and submits on Enter.
- **Sort in "To review"**: AI-genuine first, then priority high→medium→low, then newest date_added. Failed triage is pinned to the top. Other tabs sort by decided_at, newest first.
- **Card** (`article.card#enquiry-{id}`):
  - Header: verdict badge (`Genuine` green / `Discard` grey-red), then the one-line **AI reason**, then a priority select (High/Medium/Low), then an offering select (active offerings plus "No match").
  - Body, line 1: **requirement summary** (bold).
  - Body, line 2 (muted): author name · headline · date posted ("2d ago") · keyword chips · poster type label ("Buyer company" / "Training agency" / "Seeks individual trainer" / "Job seeker / self-promo" / "Other").
  - Extracted details (only the non-null ones are shown): Company, Contact person, Email (mailto), Phone, City, Mode, Dates, Topic.
  - "Open post ↗" link (new tab, `rel="noopener"`).
  - `<details>` "Full post text" with the raw text (`white-space: pre-wrap`).
  - Footer: checkbox (bulk), then **Accept** (primary), then **Discard** (opens an inline reason select: "Looking for individual trainers" / "Training agency / marketplace" / "Too specific / not our offering" / "Not a requirement" / "No response" / "Other — use AI reason"; it defaults to the AI reason when the AI said discard), then the muted cost text "AI $0.0011 · 1.3k tok".
  - When a decision disagreed with the AI, an "Overrode AI" badge is shown on Accepted and Discarded cards. Accepted cards show "Prospect · New". Discarded cards show the discard reason and an **Accept instead** button. Accepted cards show **Discard instead**.
  - Failed triage: the verdict area shows "AI triage failed — <error>" plus a **Retry triage** button, which shows a spinner "Asking AI…" while running.
- **Bulk bar** (`.bulk-bar#bulk-bar`, sticky, appears when ≥1 is selected): "N selected" · **Accept selected** · **Discard selected** (+ reason select) · a "Select all on page" checkbox.
- **Empty states**:
  - To review, with no runs ever: "No enquiries yet. Add keywords and connect LinkedIn in Setup, then press Run now." with a [Go to Setup] button.
  - To review, with runs done: "All caught up — nothing to review."
  - Accepted: "Accepted enquiries become prospects here."
  - Discarded: "Discarded enquiries are kept here and stay searchable."
  - A search with no hits: "No enquiries match '<q>'."
- Pagination: 25 per page, with Prev/Next.

## Runs (`/runs`)

- **Run panel** (`#run-panel`):
  - Idle: a **Run now** button (`#run-now`), or the same button disabled with the reason text (e.g. "Connect LinkedIn first", "Daily run limit reached (3/3)"). Below it, the caps line: "Today: 1/3 runs · 42/150 posts · max 10 keywords × 15 posts per run".
  - Running: progress `<progress>` keywords_done/keywords_planned, the current_step text, counters "Seen 9 · New 7 · Duplicates 2 · Triaged 5", and elapsed time. It polls every 1s.
  - Finished: an outcome line. Succeeded: "Run finished — 7 new enquiries" + [Open inbox]. Aborted: a red "Run stopped: LinkedIn checkpoint detected" + guidance. Failed: the error message.
- **History table** (`#run-history`): Started · Status badge · Keywords · Seen · New · Dupes · Triaged · Cost · Duration, with each row linking to the detail. Empty: "No runs yet."
- **Run detail** (`/runs/{id}`): the totals and cost, then the per-keyword table (`#run-keywords`): Keyword · Status · Seen · New · Dupes · Errors · Duration · Message (drift warnings appear here).

## Setup (`/setup`)

- **LinkedIn connection card** (`#linkedin-card`):
  - The current status with an explanation. Not connected: "sales0 uses your team's own LinkedIn login in a dedicated browser profile on this computer. Log in once; we never store your password."
  - **Connect LinkedIn** (`#connect-linkedin`). While connecting: "A browser window opened — log in to LinkedIn there. Waiting… 0:42", with [Cancel].
  - **Re-check** (`#check-linkedin`).
  - The last checked and last successful run times.
  - In needs_attention, the last_error and instructions.
- **Keywords card**: an add form (`#keyword-input` + [Add keyword]) and a list (`#keyword-list`). Each row has the text, an active toggle, the last run time and a Remove button (with confirm). Empty: "No keywords yet — add 10–20 phrases buyers use, e.g. 'corporate AI training'."
- **Offerings card**: a list grouped by group (`#offering-list`), each row with name, description, Edit and Delete. Delete confirms "Delete '<name>'?"; an offering used by enquiries is deactivated instead, with a toast. An add form takes name, group (text input with a datalist of existing groups) and description.
- **Safety limits** (read-only): the effective caps and delays, with the note "Change these in .env".

## Stub pages

A heading, a `.badge--stub` "Coming in Phase N", one sentence on what it will do, and no controls. The text includes "Coming in Phase N".

## Test hooks (stable ids used by E2E)

`#status-light`, `#cost-badge`, `#alert-banner`, `#toasts`, `#inbox-tabs`, `#inbox-search`, `#inbox-list`, `#enquiry-{id}` (plus `data-urn` holding the first source ref, and `data-verdict`, `data-review-status`), `#bulk-bar`, `#run-panel`, `#run-now`, `#run-history`, `#run-keywords`, `#linkedin-card`, `#connect-linkedin`, `#check-linkedin`, `#keyword-input`, `#keyword-list`, `#offering-list`.
