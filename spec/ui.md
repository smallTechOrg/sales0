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

- **Title**: "Review inbox". Tabs (`#inbox-tabs`): **To review (n)** · **Accepted (n)** · **Discarded (n)** · **Skipped by AI (n)** (Phase 1.4). A search box (`#inbox-search`, placeholder "Search text, company, author…") searches within the current tab and submits on Enter.
- **Phase 1.4 — the inbox holds only relevant posts.** Every card in To review / Accepted / Discarded is an AI-genuine post (or one a human rescued). The AI's discards are not cards; they live in the fourth tab:
  - **Skipped by AI (n)** (`?tab=skipped`, `#inbox-tab-skipped`): a compact searchable list (`#skipped-list`, same `#inbox-search`; case-insensitive over snippet, reason, author). A sub-filter `All skipped | Triage failed (n)` (`#skipped-state`). Row (`article.skipped#skipped-{id}`): the snippet (≤ 240 chars, muted), the one-line AI reason, poster-type label, author · headline, keyword chip, "seen 3× · last 2h ago", and the post link when one was captured. Buttons: **Rescue → enquiry** (`.btn--ghost`: moves it to To review; toast "Moved to To review") and **Override: accept** (`.btn--primary`: rescue plus accept, recorded as an override). Both are disabled with the title "Post text is no longer stored (kept 14 days)" once `can_rescue` is false; the row then shows "text purged". A failed-triage row shows the error and **Retry triage**. Pagination 25.
  - Empty states: "Nothing skipped yet — posts the AI judges not relevant are listed here." / search: "No skipped posts match '<q>'." / failed: "No triage failures."
  - The tab count is `counts.skipped`. The Skipped list never has bulk controls.
- **Sort in "To review"**: AI-genuine first, then priority high→medium→low, then newest date_added. Failed triage is pinned to the top. Other tabs sort by decided_at, newest first.
- **Card** (`article.card#enquiry-{id}`):
  - Header: verdict badge (`Genuine` green / `Discard` grey-red), then the one-line **AI reason**, then a priority select (High/Medium/Low), then an offering select (active offerings plus "No match").
  - Body, line 1: **requirement summary** (bold).
  - Body, line 2 (muted): author name · headline · date posted ("2d ago") · keyword chips · poster type label ("Buyer company" / "Training agency" / "Seeks individual trainer" / "Job seeker / self-promo" / "Other").
  - Extracted details (only the non-null ones are shown): Company, Contact person, Email (mailto), Phone, City, Mode, Dates, Topic.
  - The source link (`a.enquiry__link`, new tab, `rel="noopener"`, `data-link-kind` = the card's `link_kind`). Phase 1.2 label by link kind (replaces Phase 1's "Open post ↗"):
    - `post` → **"View post ↗"**
    - `profile` → **"Author profile (post link unavailable) ↗"**, followed (Phase 1.4) by the muted "· posted ~3h before sales0 found it" when `posted_relative` is known
    - Phase 1.4: a genuine post's link is captured right after triage for every card layout, so "View post ↗" is the common case; the profile fallback appears only when LinkedIn's menu gave no link (cap, 3 misses, or the card had no menu).
    - `search` → **"Search results (post link unavailable) ↗"**
    - `other` → "Open link ↗"
    - no `source_url` → no link.
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
  - Idle: a **Run now** button (`#run-now`), or the same button disabled with the reason text (e.g. "Connect LinkedIn first", "Daily run limit reached (3/3)", or, Phase 1.1, "LinkedIn asked us to slow down — next run allowed after 14:35"). Below it, the caps line: "Today: 1/3 runs · 42/150 posts · max 10 keywords × 15 posts per run".
  - Running: progress `<progress>` keywords_done/keywords_planned, the current_step text, counters "Seen 9 · New 7 · Duplicates 2 · Triaged 5", and elapsed time. It polls every 1s. The window stays invisible ("Runs in the background — no browser window").
  - **Phase 1.4 run summary** (`#run-summary`, running and finished, same numbers): **Scanned** (posts_seen) · **New to us** (posts_new) · **Already seen** (posts_dupe) · **Relevant** (posts_relevant) · **Skipped by AI** (posts_skipped_ai) · **Post links** (permalinks_found) · **Relevance** ("6% of new posts") · **AI cost**. Wording is honest: "Scanned 312 posts · 9 relevant (6% of the 140 new ones)". Runs are long (1–3 h with the defaults): the idle panel says "Run now starts a long background run — you can close this tab; progress is kept." and the running panel adds "Keyword 12/40" with the current keyword and elapsed time. **Run now** returns at once. After a server restart the interrupted run reads "Interrupted by server restart — the next run continues with the keywords it had not reached" (an amber outcome, not red).
  - The caps line becomes "Today: 1/4 runs · 612/2000 posts scanned · max 40 keywords × 80 posts per run".
  - Finished: an outcome line. Succeeded: "Run finished — 7 new enquiries" + [Open inbox]. Aborted: a red "Run stopped: LinkedIn checkpoint detected" + guidance, plus (Phase 1.1) the cool-down line "Next run allowed after HH:MM" when one applies. Failed: the error message. For the not-logged-in failure the guidance is a [Go to Setup] link.
  - **Drift alert (Phase 1.1)** (`#run-drift-alert`, `.banner` with the warn style, `role="status"`). It appears on a finished run with `layout_warnings > 0`, both in the panel and in the run detail: "LinkedIn's page layout may have changed — N keyword(s) returned results sales0 couldn't fully read. Some posts may be missing. See the per-keyword log." It links to `/runs/{id}#run-keywords`. It is not dismissible. Runs stay allowed.
- **History table** (`#run-history`): Started · Status badge · Keywords · Scanned · New · Relevant · Skipped · Cost · Duration (Phase 1.4; was Seen · New · Dupes · Triaged), with each row linking to the detail. Empty: "No runs yet." (Phase 1.1) A run with `layout_warnings > 0` shows a `.badge--warn` "Layout warning" next to its status badge.
- **Run detail** (`/runs/{id}`): the drift alert when it applies, then the totals and cost, then the per-keyword table (`#run-keywords`): Keyword · Status · Scanned · New · Already seen · Relevant · Skipped · Links · Cost · Duration · Message (Phase 1.4; the run-level `#run-summary` sits above it). Drift warnings, field-miss counts and unreadable-card counts appear in the Message column. A `warning` row uses the `warning` status badge.

## Setup (`/setup`)

- **LinkedIn connection card** (`#linkedin-card`). Phase 1.1 copy is binding:
  - The current status with an explanation. Not connected: "sales0 opens Google Chrome with its own sales0 profile on this computer. Log in to LinkedIn there once — your everyday Chrome profile isn't touched, and sales0 never sees or stores your password."
  - **Browser line** (`#browser-note`, always shown): "Browser: Google Chrome 156" (from `session.browser_label`). When `session.browser_note` is set, it shows that note instead, in the warn style: "Google Chrome not found — using the built-in browser". When channel `chrome` is configured but missing, it shows the `BrowserUnavailable` message.
  - **Connect LinkedIn** (`#connect-linkedin`). While connecting:
    - Line 1, with spinner and elapsed time: "A Google Chrome window opened. Waiting… 0:42". In the fallback it reads "A browser window opened".
    - Line 2, exact text: **"Log in and finish LinkedIn's 2FA in the Chrome window — this is one time only."** In the fallback it reads "…in the browser window — this is one time only."
    - Line 3 (muted): "It closes by itself a moment after you reach your LinkedIn feed. If Chrome shows “Chrome is being controlled by automated test software”, that's sales0's window — it's safe to continue."
    - [Cancel].
  - Connected: "LinkedIn is connected. Checks and runs reuse this login invisibly in the background — no window opens."
  - **Re-check** (`#check-linkedin`): "Checking your LinkedIn session in the background…" while busy.
  - The last checked and last successful run times.
  - In needs_attention, the last_error and the instruction "Press Connect LinkedIn — the sales0 Chrome window opens on LinkedIn so you can resolve it there. Runs stay paused until then." When a cool-down is active (`session.cooldown_until`), it adds "After that, the next run is allowed after HH:MM."
  - A connect failure (timeout, window closed, browser unavailable) shows `last_error` in the error style above the buttons.
- **Keywords card**: an add form (`#keyword-input` + [Add keyword]), **[Add suggested keywords]** (`#add-suggested-keywords`; Phase 1.4) and a list (`#keyword-list`). Each row has the text, an active toggle, the last run time and a Remove button (with confirm). Empty: "No keywords yet — add 10–20 phrases buyers use, e.g. 'corporate AI training'." Phase 1.4:
  - The list is grouped under `<details>` headings, one per offering (Training · AI corporate workshops, …) plus "Your keywords"; each heading shows "n keywords · scanned S · relevant R".
  - A row also shows the yield "Scanned 412 · Relevant 6 (1.5%) · last run 2h ago" (`.muted`, `.mono` numbers), a `.badge--stub`-style "No yield" hint when ≥ 80 scanned and 0 relevant, an **Edit** button (inline form: text + group select; Save/Cancel) and a "suggested" tag on seeded rows.
  - The empty state offers the button: "No keywords yet — press Add suggested keywords for 60 phrases buyers use, or add your own."
  - Pressing the button toasts "Added N · M already present"; it never changes an existing row.
- **Offerings card**: a list grouped by group (`#offering-list`), each row with name, description, Edit and Delete. Delete confirms "Delete '<name>'?"; an offering used by enquiries is deactivated instead, with a toast. An add form takes name, group (text input with a datalist of existing groups) and description.
- **Safety limits** (read-only): the effective caps and delays, with the note "Change these in .env". The Phase 1.1 rows are in `architecture.md` → Configuration → Phase 1.1 config changes.

## Stub pages

A heading, a `.badge--stub` "Coming in Phase N", one sentence on what it will do, and no controls. The text includes "Coming in Phase N".

## Test hooks (stable ids used by E2E)

`#status-light`, `#cost-badge`, `#alert-banner`, `#toasts`, `#inbox-tabs`, `#inbox-search`, `#inbox-list`, `#enquiry-{id}` (plus `data-urn` holding the first source ref, and `data-verdict`, `data-review-status`), `#bulk-bar`, `#run-panel`, `#run-now`, `#run-history`, `#run-keywords`, `#linkedin-card`, `#connect-linkedin`, `#check-linkedin`, `#keyword-input`, `#keyword-list`, `#offering-list`. Phase 1.1 adds `#browser-note` and `#run-drift-alert`. Phase 1.2 adds `a.enquiry__link[data-link-kind]` inside `#enquiry-{id}`; `data-urn` may now hold a `ck:<KEY>` ref. Phase 1.4 adds `#add-suggested-keywords`, `#inbox-tab-skipped`, `#skipped-list`, `#skipped-state`, `article#skipped-{id}` (with `data-verdict="discard"` and `data-can-rescue`), buttons `.skipped__rescue` / `.skipped__override` / `.skipped__retry`, `#run-summary` (with `data-scanned`, `data-new`, `data-relevant`, `data-skipped` attributes), and `[data-keyword-id]` keyword rows with `.keyword__yield`.

**CSS (Phase 1.1):** `app.css` adds `.banner--warn` (warn colours from the existing `--color-warn` / `--color-warn-soft` tokens, AA contrast), used by `#run-drift-alert`. Existing classes are reused otherwise: `.badge--warn`, `.outcome--warn`.
