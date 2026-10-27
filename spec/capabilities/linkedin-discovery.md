# Capability: LinkedIn Discovery (Connect, Run now, Dedupe, Run Log, Safety)
**Phase 1**

## What It Does
Connects the team's own LinkedIn session once, then on **Run now** searches LinkedIn posts for each active keyword. It runs slowly and like a human, merges duplicates into single enquiries, hands new enquiries to AI triage, and logs every run. It stops at the first sign of trouble with the account.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Connect click | action | Setup | Yes (once) |
| Team login | credentials typed by the user in the opened browser window (never seen by sales0) | User | Yes (once) |
| Run now click | action | Runs page | Yes |
| Active keywords | list | Search Setup | Yes (≥1) |
| Safety limits | numbers | Configuration (`.env`) | Defaults apply |
| Mode | live \| fixture | Configuration | Default live |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Session status | not connected / connected / needs attention (+ busy) | Header light, Setup card, alert banner |
| Run record | status, per-keyword counts, errors, duration, abort reason, cost | Runs page, run detail |
| Enquiries | one per distinct post (raw text, author, link, date, keywords) | Triage → Review inbox |
| Live progress | current keyword/step, counters | Runs page (refreshes every second) |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| LinkedIn (team session, persistent browser profile) | open feed (session check), content search results by keyword, scroll, expand "see more" | Logout/authwall, checkpoint, captcha or rate-limit (HTTP 429/999): **stop the run immediately**, close the browser, set "needs attention" with the reason, show the banner, never retry automatically. Other page errors: mark that keyword `error`, continue to the next keyword. |
| AI triage (see ai-triage) | per new enquiry | Enquiry marked "triage failed" with Retry. The run continues. A fatal key/model error stops triage and fails the run with a clear message. |

## Business Rules
- **Connect** opens a visible browser window on the dedicated profile. The user logs in there (including any 2FA). sales0 detects the logged-in state within ~2s of it appearing and closes the window. It gives up after the configured timeout (default 5 min) with the message "Login not completed — try again". The user can cancel. Credentials are never typed, read or stored by sales0. Only the browser profile persists, under the gitignored data directory.
- **Re-check** verifies the session in the background without logging in.
- Only one LinkedIn activity (connect, check, run) happens at a time. A second request is refused with "LinkedIn is busy".
- **Run now is allowed only when** the session is connected (not needs-attention), no run is active, there is ≥1 active keyword, the runs started today are below the daily cap, and the posts seen today are below the daily cap. Otherwise the button is disabled with the exact reason.
- Each run first re-verifies the session. Keywords are taken least-recently-run first, up to the per-run keyword cap. Each keyword collects at most the per-keyword post cap, with at most N scrolls and a recent date filter (default past week).
- Pacing: a random delay within the configured min/max between page actions, and a longer random pause between keywords. The live window is visible by default.
- The daily post cap is checked after each keyword. Reaching it ends the run normally, with "Daily post limit reached" and the remaining keywords marked skipped.
- **Dedupe**: a post whose LinkedIn id was seen before is a duplicate. A post with a new id but the same normalised text as an existing enquiry is also a duplicate. Normalisation: Unicode NFKC, lower-case, URLs removed, hashtags removed, whitespace collapsed. A duplicate adds its keyword to the existing enquiry (and its new source id, for content duplicates). It is never re-triaged and never creates a second card.
- New enquiries are saved before triage. Raw text is kept forever.
- On a safety stop: posts already saved are kept and still triaged, the remaining keywords are marked skipped, and the session becomes "needs attention" with the reason and page. It goes back to "connected" only after a successful Connect or Re-check.
- A logged-in results page with neither results nor a "no results" marker is flagged as a possible layout change on that keyword's log row (a warning, not a stop).
- Runs interrupted by a server restart are marked failed at the next startup.
- **Fixture mode** runs the identical flow against a built-in fake LinkedIn with a known post corpus. It is used for automated tests and offline demos, and is labelled "FIXTURE MODE" in the header.
- Scheduling is out of scope (Phase 4). Phase 1 runs only on a button press.

## Success Criteria
- [ ] Fixture mode: Connect makes the status "connected" within 10s, and the header light shows "LinkedIn connected".
- [ ] Fixture run 1 (keywords `corporate ai training`, `ai workshop`, `ai consulting`) ends `succeeded` with seen 9 / new 7 / dupes 2 / triaged 7 (`architecture.md` → corpus).
- [ ] The enquiry for P1 carries both keyword chips. P2 has 2 source refs (P2 + P7 URNs). There are exactly 7 enquiry rows.
- [ ] Run 2 after adding `claude code training`: seen 11 / new 2 / dupes 9, and the new keyword is searched first.
- [ ] Keyword `checkpoint-test` (placed first): the run ends `aborted` with reason `checkpoint`, the session is `needs_attention`, the other keywords are `skipped`, `/inbox` renders `#alert-banner`, and **Run now** is disabled with the needs-attention reason.
- [ ] `captcha-test` → aborted `captcha`. `ratelimit-test` → aborted `rate_limited`. A cleared profile (no cookie) → aborted `logout`.
- [ ] With `SALES0_SCRAPE_MAX_RUNS_PER_DAY=1`, a second Run now is refused with `daily_run_cap`.
- [ ] With `SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD=2`, every keyword row reports seen ≤ 2.
- [ ] A Run now while a run is active is refused with `run_in_progress`. Connect during a run is refused with `job_busy`.
- [ ] The Runs page progress panel shows changing counters while the run is active, without a page reload.
- [ ] A server restart during a run → that run shows `failed` "Interrupted by server restart".
- [ ] In live mode, `/__fixtures/linkedin/feed/` returns 404.
- [ ] Every keyword produces a `scrape_keyword` JSON log line, and every run produces a `scrape_run` line.
