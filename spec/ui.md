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
- **Nav (in order)**: Inbox · Runs · Tracked · Setup · Pipeline · Contacts · Companies · Competitors & Groups · Import · Sync · Outreach · Replies · Metrics · Digest · Suggestions · Costs · Schedule · Users (all real); then WhatsApp, the only stub (not built, deferred by decision), with a `.badge--stub` "Not built". On narrow screens the nav is one horizontally scrolling row. Pipeline shows a count badge (`#due-badge`) while follow-ups are due or overdue. (Phase 2: the four former Phase-2 stubs are real pages and "Sync" is new; the nav tests are updated accordingly.)
- **Header, right side (Phase 2):** when a password is configured a "Log out" button (`#logout`, a POST form) sits after the acting-as selector.
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
    - `search` → **"Search results (post and profile links unavailable) ↗"**, shown only when no author profile is known (else the profile link; inbox feedback A2)
    - `other` → "Open link ↗"
    - no `source_url` → no link.
  - `<details>` "Full post text" with the raw text (`white-space: pre-wrap`).
  - Footer: checkbox (bulk), then **Accept** (primary), then **Discard** (opens an inline reason select: "Looking for individual trainers" / "Training agency / marketplace" / "Too specific / not our offering" / "Not a requirement" / "No response" / "Other — use AI reason"; it defaults to the AI reason when the AI said discard), then the muted cost text "AI $0.0011 · 1.3k tok".
  - When a decision disagreed with the AI, an "Overrode AI" badge is shown on Accepted and Discarded cards. Accepted cards show "Prospect · New". Discarded cards show the discard reason and an **Accept instead** button. Accepted cards show **Discard instead**.
  - Failed triage: the verdict area shows "AI triage failed — <error>" plus a **Retry triage** button, which shows a spinner "Asking AI…" while running.
- **Inbox feedback** (`capabilities/inbox-feedback.md`): "Already in pipeline: <prospect>" line (`.enquiry__pipeline`) linking the prospect; "Auto-skipped: repeat" badge on system discards; the discard panel adds "Or your own reason" (`custom_reason`) and, for admins, "Save as a triage rule (exclude)" (`save_rule`).
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

## Pipeline (`/pipeline`) — Phase 2
- **Title** "Pipeline". Tabs (`#pipeline-tabs`): **Prospects (n)** · **Leads (n)** · **Clients (n)**; a **Board | List** toggle (`#view-toggle`) on Prospects and Leads; filters: search `#pipeline-search`, stage select, flag select (Repeat poster / Mentions competitor / Do not follow up), source select (Enquiry / Sheet / Manual / Import), status select (Open default · Closed · All), "Show removed from sheet" checkbox (off).
- **Due today panel** (`#due-today`, above the tabs, `aria-live="polite"`): two lists, "Due today (n)" and "Overdue (n)". Row: prospect/lead name (link), reminder label ("Follow-up 1"), due date, note, buttons **Done** (marks done and moves the prospect to that stage), **Snooze** (date input), **Open**. Empty: "Nothing due today." Prospects with the Do-not-follow-up flag never appear.
- **Board** (`#pipeline-board`): one column per stage in order (Prospects: New · Contacted · Follow-up 1 · Follow-up 2 · Final follow-up; Leads: Meeting scheduled · Proposal sent · Won · Lost · No response), header with the stage name and total, cards (`article.pcard#prospect-{id}` / `#lead-{id}`, `data-stage`): name, company, "contacted 3d ago", next follow-up date (red when overdue), flag badges (`.flag--repeat_poster` "Repeat poster ×3", `.flag--competitor_mention` "Mentions Future Labs", `.flag--no_follow_up` "Do not follow up"), source chips ("Enquiry", "Outreach", "Prospects", "Working Sheet" with a small "Synced from sheet" mark), tracked indicator. Each card has a stage `<select>` (moves it; htmx) — no drag and drop. 50 cards per column, "Show all" opens the list filtered to that stage.
- **List** (`#pipeline-list`): table Name · Company · Stage · Last activity · Next follow-up · Flags · Source · Contact; 50 per page; empty: "No prospects yet — accept an enquiry in the Inbox, or add one." with a [New prospect] button (inline form: name, company, email, phone, LinkedIn URL, website, notes).
- **Prospect detail** (`/pipeline/prospects/{id}`): header (name, company, stage badge, status, flags, `tracked` toggle, source chips); actions in order: stage select + [Move], **Response received** (`#btn-response`), **Convert to lead** (`#btn-convert`; disabled with a title explaining "Contact it first" / "Record a response first"), **Draft email** (`#btn-draft`, link to `/outreach/compose?prospect_id=ID`), Close (reason select) / Reopen, "Do not follow up" toggle. Sections: **Timeline** (`#timeline`, newest first, icon + sentence + actor + time; sheet events say "Updated from the Google Sheet (fields: status, notes)"), **Add note** (`#note-form`, textarea + [Add note]), **Follow-ups** (`#follow-ups`, open reminders with Done/Snooze, [Add follow-up] date + note), **Linked enquiries** (summary, priority, status, link to the post, "Open in Inbox"), **Contacts**, **Outreach** (messages with state badges and reply badges, links to `/outreach/messages/{id}`), **Replies**, **Synced from sheet** panel (only when sheet-fed: tab name chips, row numbers, last seen; sheet-owned fields show a lock "Sheet cell wins when it changes" and the panel says "Synced from sheet - a sheet cell wins only when it changes" (see `capabilities/crm-entity.md` B3); "Removed from the sheet on <date>" banner when `sheet_removed_at`).
- **Lead detail** (`/pipeline/leads/{id}`): stage stepper (Meeting scheduled → Proposal sent → Won / Lost / No response), form for meeting time, proposal sent date, value estimate (INR), outcome note; timeline, notes, follow-ups, linked prospect. Won shows "Client created →" link.
- **Clients tab / detail:** list of active and legacy clients; [Add legacy client] form; detail with notes, timeline, linked competitor.
- States: loading uses the htmx indicator; errors are toasts (`DomainError` message); every list has an empty state with the next action.

## Contacts (`/contacts`) — Phase 2
- Search box `#contacts-search` (htmx, 250 ms debounce, placeholder "Search name, email, company, tag…"), tag filter chips (`#tag-filter`, from `list_tags` with counts), company filter; table `#contact-list`: Name · Email · Phone · Company · Tags · Links; 50 per page; [New contact] form; link **Possible duplicates (n)** → `/contacts/duplicates`.
- **Contact detail:** editable name/title/notes/company, all emails and phones (primary marked), LinkedIn link, tag editor (`#tag-form`: name + category select; chips with ×), links to prospects/leads/clients/enquiries/competitors (`.contact-link`), timeline.
- **Duplicates** (`#merge-list`): each group shows its contacts side by side with the reason ("share the email a@b.com"); **Merge into this one** (`#merge-form`, `hx-confirm` "Merge B into A? This cannot be undone.").
- Empty: "No contacts yet — they are created when you accept an enquiry or send an email." No results: "No contacts match '<q>'."

## Competitors & Groups (`/competitors`) — Phase 2
Two cards. **Competitors** (`#competitor-list`): table Name · Website · Approx. clients · Client names · Contact · Mentions (n, link) · Active, [Add competitor] inline form, Edit/Delete per row; sheet-fed rows show "Synced from sheet" on sheet-owned fields (edit allowed for notes/active; Delete disabled with the `sheet_owned` explanation). **Groups** (`#group-list`): Name · Platform · Link · Keep level badge ("Keep" / "Keep (low)") · Active toggle, add/edit/delete. A "Removed from sheet" subsection (hidden unless `show=removed`). Competitor detail lists the linked enquiries and clients and lets the user link a client.

## Import (`/import`) — Phase 2
Two cards: **Paste a post** (`#paste-form`: textarea ≥ 40 chars, optional link, author, author profile URL; [Add to inbox] runs one triage call with a spinner, then lands on To review with a toast "Added — AI says Genuine"; duplicate: "Already in your inbox" with a link) and **Import a CSV** (`#csv-form`: file + kind select (Enquiries, Discarded enquiries, Outreach, Prospects, Working sheet, Competitors, Groups, Legacy clients, Contacts) + [Import]). A note: "Your Google Sheets are synced automatically (see Sync) — import is for data that is not in them." Recent batches table; batch page (`#import-summary`): total / new / merged / unchanged / errors and a table of errors (row, code) — no cell content.

## Sync (`/sync`) — Phase 2
Heading "Google Sheet sync", a `.badge` "Read-only: sales0 never writes to your sheets". Panel `#sync-status`: state (Idle / Running… with spinner / Disabled — "SALES0_SHEET_SYNC_MINUTES is 0"), "Last sync 12 min ago (ok|partial|failed) · took 8s", "Next sync in ~18 min", last error in the error style. Button **Sync now** (`#sync-now`; disabled while running). Table `#sync-tabs`: Tab · Rows · Added · Updated · Unchanged · Removed · Errors · Warnings · Problem (fetch error / "removal blocked 6-of-10") — counts only. Empty: "No sync has run yet." Links: Schedule page (opt-in toggle for the interval job), Pipeline.

## Criteria (`/criteria`) — inbox feedback A3
Include list (`#include-list`), exclude list (`#exclude-list`), add form (`#new-rule`, admins), per-rule ↑ ↓ / Disable-Enable / Edit (admins), "Why posts were discarded" (`#discard-reasons`: Team / System / AI, 30 days, all time, example posts), recent versions (`#criteria-history`). Members see the lists and the breakdown only.

## Login (`/login`)
Centered card: wordmark, **Email** field (`#login-email`) and **Password** field (`#login-password`, `autocomplete="current-password"`), Remember me, [Log in] (`#login-submit`), a hint saying which email to use (the admin email on a fresh install) and that an admin supplies set-password links; error in the error style above the field ("Wrong email or password." / "Too many attempts - try again in 42 seconds."); no nav, no header status; same CSS. Keyboard: Enter submits.

## Stub pages

A heading, a `.badge--stub` "Not built — deferred by decision", one sentence on what it would do, and no controls. **WhatsApp** is the only stub.

## Test hooks (stable ids used by E2E)

`#status-light`, `#cost-badge`, `#alert-banner`, `#toasts`, `#inbox-tabs`, `#inbox-search`, `#inbox-list`, `#enquiry-{id}` (plus `data-urn` holding the first source ref, and `data-verdict`, `data-review-status`), `#bulk-bar`, `#run-panel`, `#run-now`, `#run-history`, `#run-keywords`, `#linkedin-card`, `#connect-linkedin`, `#check-linkedin`, `#keyword-input`, `#keyword-list`, `#offering-list`. Phase 1.1 adds `#browser-note` and `#run-drift-alert`. Phase 1.2 adds `a.enquiry__link[data-link-kind]` inside `#enquiry-{id}`; `data-urn` may now hold a `ck:<KEY>` ref. Phase 1.4 adds `#add-suggested-keywords`, `#inbox-tab-skipped`, `#skipped-list`, `#skipped-state`, `article#skipped-{id}` (with `data-verdict="discard"` and `data-can-rescue`), buttons `.skipped__rescue` / `.skipped__override` / `.skipped__retry`, `#run-summary` (with `data-scanned`, `data-new`, `data-relevant`, `data-skipped` attributes), and `[data-keyword-id]` keyword rows with `.keyword__yield`.

**Phase 2 test hooks:** `#due-today`, `#due-badge`, `#pipeline-tabs`, `#view-toggle`, `#pipeline-search`, `#pipeline-board` (columns `[data-stage]`), `#pipeline-list`, `article#prospect-{id}` / `#lead-{id}` (`data-stage`), `.flag--repeat_poster`, `.flag--competitor_mention`, `.flag--no_follow_up`, `#btn-response`, `#btn-convert`, `#btn-draft`, `#timeline`, `#note-form`, `#follow-ups`, `#contacts-search`, `#contact-list`, `#tag-filter`, `#tag-form`, `#merge-list`, `#merge-form`, `#competitor-list`, `#group-list`, `#paste-form`, `#csv-form`, `#import-summary`, `#sync-now`, `#sync-status`, `#sync-tabs`, `#login-form`, `#login-password`, `#login-submit`, `#logout`.

**CSS (Phase 1.1):** `app.css` adds `.banner--warn` (warn colours from the existing `--color-warn` / `--color-warn-soft` tokens, AA contrast), used by `#run-drift-alert`. Existing classes are reused otherwise: `.badge--warn`, `.outcome--warn`.
