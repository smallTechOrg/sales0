# Capability: LinkedIn Discovery (Connect, Run now, Dedupe, Run Log, Safety)
**Phase 1, reworked in Phase 1.1 (Real-Chrome connect + robust LinkedIn detection)**

## What It Does
Connects the team's own LinkedIn session once, by logging in to a real Google Chrome window that sales0 opens. Then, on **Run now**, it searches LinkedIn posts for each active keyword, invisibly in the background. It runs slowly and like a human, merges duplicates into single enquiries, hands new enquiries to AI triage, and logs every run. It stops at the first sign of trouble with the account and tells the user when LinkedIn's page layout seems to have changed.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Connect click | action | Setup | Yes (once) |
| Team login | credentials and 2FA typed by the user in the opened Google Chrome window (never seen by sales0) | User | Yes (once) |
| Run now click | action | Runs page | Yes |
| Active keywords | list | Search Setup | Yes (≥1) |
| Safety limits | numbers | Configuration (`.env`) | Defaults apply |
| Mode | live \| fixture | Configuration | Default live |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Session status | not connected / connected / needs attention (+ busy), plus which browser is used | Header light, Setup card, alert banner |
| Run record | status, per-keyword counts, errors, duration, abort reason, cost, layout warnings | Runs page, run detail |
| Layout-change alert | count of keywords whose results could not be read | Runs panel, run detail, logs |
| Enquiries | one per distinct post (raw text, author, link, date, keywords) | Triage → Review inbox |
| Live progress | current keyword/step, counters | Runs page (refreshes every second) |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| LinkedIn (team session, sales0's own Google Chrome profile on this computer) | open feed (session check), content search results by keyword, scroll, expand "see more" | Each of these makes sales0 **stop the run immediately**, close the browser, set "needs attention" with the reason, show the banner and never retry automatically: logout (sent to a login page or the authwall, or the session cookie disappears), a checkpoint/challenge page, a captcha, or a rate limit (HTTP 429/999 on any LinkedIn response, including background requests the page makes). It checks before each keyword and after every page load, scroll and "see more". Other page errors: that keyword is marked `error` and the run continues with the next keyword. |
| AI triage (see ai-triage) | per new enquiry | Enquiry marked "triage failed" with Retry. The run continues. A fatal key/model error stops triage and fails the run with a clear message. |

## Business Rules
- **Connect** opens a normal **Google Chrome** window: the Chrome installed on this computer, not a separate-looking browser. It runs on sales0's own dedicated Chrome profile, which is separate from the user's everyday Chrome profiles. Those are never read or changed. The user logs in there and finishes LinkedIn's 2FA, including any "remember this browser" step. **This happens once.**
  - sales0 recognises the login within about 2 seconds of the user reaching the feed or any logged-in LinkedIn page. It confirms the login once by loading the feed, then closes the window.
  - Recognition depends on LinkedIn's session cookie and on the page address (not a login, 2FA or security page), never on how LinkedIn's page looks.
  - It gives up after the configured timeout (default 5 min) with "Login not completed — try again". The user can cancel.
  - Credentials are never typed, read or stored by sales0. Only the browser profile persists, under the gitignored data directory.
- If Google Chrome is not installed, sales0 uses its built-in browser instead and says so on Setup: "Google Chrome not found — using the built-in browser".
- Chrome may show a bar saying it "is being controlled by automated test software". That is sales0's window, and Setup says so.
- **Re-check** verifies the session invisibly in the background (no window), without logging in.
- **Runs are invisible.** Every check and run uses the same sales0 Chrome profile with no window.
- Only one LinkedIn activity (connect, check, run) happens at a time. The profile is never opened twice. A second request is refused with "LinkedIn is busy".
- **Run now is allowed only when** all of these hold. Otherwise the button is disabled with the exact reason.
  - The session is connected (not needs-attention).
  - No run is active.
  - No safety cool-down is in effect.
  - There is ≥1 active keyword.
  - The runs started today are below the daily cap.
  - The posts seen today are below the daily cap.
- Each run first re-verifies the session. If sales0's browser has no LinkedIn login at all, the run fails at once with "Not logged in to LinkedIn in sales0's browser — press Connect LinkedIn in Setup". This happens, for example, the first time after upgrading from Phase 1. No LinkedIn page is opened, so it is not a safety stop and starts no cool-down.
- Keywords are taken least-recently-run first, up to the per-run keyword cap. Each keyword collects at most the per-keyword post cap, with at most N scrolls and a recent date filter (default past week).
- Pacing: a random delay within the configured min/max between page actions, and a longer random pause between keywords.
- The daily post cap is checked after each keyword. Reaching it ends the run normally, with "Daily post limit reached" and the remaining keywords marked skipped.
- **Dedupe**: a post whose LinkedIn id was seen before is a duplicate. A post with a new id but the same normalised text as an existing enquiry is also a duplicate. Normalisation: Unicode NFKC, lower-case, URLs removed, hashtags removed, whitespace collapsed. A duplicate adds its keyword to the existing enquiry (and its new source id, for content duplicates). It is never re-triaged and never creates a second card.
- New enquiries are saved before triage. Raw text is kept forever.
- On a safety stop: posts already saved are kept and still triaged, the remaining keywords are marked skipped, and the session becomes "needs attention" with the reason and page. It goes back to "connected" only after a successful Connect or Re-check. Connect opens the sales0 Chrome window on LinkedIn, so the user can resolve a checkpoint there.
- **Safety cool-down.** Even after the session is green again, Run now stays refused for a while after a safety stop: 60 minutes after a rate-limit or captcha, and 15 minutes after a checkpoint or logout. Both are configurable; 0 turns the cool-down off. The reason reads "LinkedIn asked us to slow down — next run allowed after HH:MM" (local time). Re-check and Connect do not clear it. It is checked right after "needs attention".
- The keyword that was being searched when a stop tripped counts as "just run", so the next run searches it last.
- **Layout changes are never silent.** A logged-in results page that yields 0 readable posts, while it has result cards or shows no "no results" message, marks that keyword `warning` "No posts parsed — LinkedIn layout may have changed". The same happens when an author, headline, time or profile link can't be read on at least half of a keyword's posts (and at least 2 posts were parsed). Smaller misses and unreadable cards are listed in the keyword's message. Any warning in a run shows a run-level alert on the Runs page and in the run detail, and is logged. It is a warning, not a stop.
- Post cards are recognised by LinkedIn's post ids and post links, and each detail has ordered fallbacks. sales0 does not depend on LinkedIn's (obfuscated) class names.
- Runs interrupted by a server restart are marked failed at the next startup.
- Deleting a keyword while a run is active does not affect that run. Its hits keep the keyword text; the link to the deleted keyword is cleared.
- **Fixture mode** runs the identical flow against a built-in fake LinkedIn with a known post corpus. It is used for automated tests and offline demos, and is labelled "FIXTURE MODE" in the header.
- Scheduling is out of scope (Phase 4). Phase 1 runs only on a button press.

## Success Criteria
- [ ] Fixture mode: Connect goes through the fixture's login → 2FA → "remember this browser" → feed flow. It makes the status "connected" within 10 s, and the header light shows "LinkedIn connected". Connect does NOT succeed while the page is on `/checkpoint/challenge/remember-browser`, even though the cookie is already set. It succeeds within 3 s of the page reaching `/feed/`.
- [ ] The fixture's logged-in feed and results pages contain no `global-nav` id or class and none of Phase 1's `update-components-*` / `feed-shared-*` / `search-reusable-*` classes. Connect, Re-check and runs all still work on them.
- [ ] `classify_page` (pure) returns the verdict listed in `tests/fixtures/linkedin_pages/manifest.json` for every saved sample: logged-in feed and results → ok; login → logout; login form served at `/feed/` → logout; authwall → logout; checkpoint → checkpoint; captcha → captcha; 429/999 → rate_limited. The same verdicts hold when each sample is loaded in a real headless browser.
- [ ] The saved current-markup results sample parses to exactly the 6 manifest posts with the pinned author, headline, time and text. The data-urn, data-id, `/feed/update/` link and `/posts/` link card shapes are all found. The reshare is not double-counted. `field_misses == {"author_headline": 1}`.
- [ ] The saved "drifted" results page gives 0 posts and `drift_warning=True`. The saved "No results found" page gives 0 posts and `drift_warning=False`.
- [ ] Fixture `layout-drift-test` → that keyword row is `warning` "No posts parsed — LinkedIn layout may have changed…". `RunView.layout_warnings == 1`, `#run-drift-alert` is rendered on the run panel and the run detail, and the `scrape_keyword` log line has `drift: true` at WARNING.
- [ ] A results page where the author can't be read on ≥ half of ≥ 2 parsed posts → keyword `warning` with "couldn't read author on M". Fewer misses → `done`, with the counts in the Message column.
- [ ] Real Chrome (headless, this Mac, throwaway profile): `navigator.webdriver` is false; the UA has no `HeadlessChrome` and contains `Chrome/156.`; the timezone equals `SALES0_TIMEZONE`.
- [ ] `uv run python -m sales0.cli check-browser` prints exactly `browser OK channel=chrome version=156.<…>` and exits 0, without opening any LinkedIn URL or the sales0 profile.
- [ ] With Chrome reported absent (stubbed resolution) and channel `auto`: Setup shows "Google Chrome not found — using the built-in browser", and Connect uses the bundled Chromium.
- [ ] Headed and headless launch options are identical except for `headless`, the viewport/window size and the UA override. `--enable-automation` is in `ignore_default_args`, `--disable-blink-features=AutomationControlled` is in `args`, and the sandbox is on (unit test on `launch_options`).
- [ ] Fixture run 1 (keywords `corporate ai training`, `ai workshop`, `ai consulting`) ends `succeeded` with seen 9 / new 7 / dupes 2 / triaged 7 (`architecture.md` → corpus).
- [ ] The enquiry for P1 carries both keyword chips. P2 has 2 source refs (P2 + P7 URNs). There are exactly 7 enquiry rows.
- [ ] Run 2 after adding `claude code training`: seen 11 / new 2 / dupes 9, and the new keyword is searched first.
- [ ] Keyword `checkpoint-test` (placed first): the run ends `aborted` with reason `checkpoint`, the session is `needs_attention`, the other keywords are `skipped`, `/inbox` renders `#alert-banner`, and **Run now** is disabled with the needs-attention reason.
- [ ] `captcha-test` → aborted `captcha`. `ratelimit-test` → aborted `rate_limited`. `ratelimit-scroll-test` (a background 429) → aborted `rate_limited`. `checkpoint-scroll-test` → aborted `checkpoint` at the scroll checkpoint. **`logout-test` (302 → `/authwall`) → aborted `logout`.** The session cookie cleared between two keywords → aborted `logout` at the next keyword's checkpoint.
- [ ] A profile with no session cookie at run start → run `failed` "Not logged in to LinkedIn in sales0's browser — press Connect LinkedIn in Setup", session `not_connected`, all keywords `skipped`, `abort_reason` null, no cool-down. No page was navigated: a context `request` listener counted 0 requests to the fixture base.
- [ ] Safety cool-down (`SALES0_SAFETY_COOLDOWN_MIN=60`, `SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN=15`, injected `now`):
  - After a `rate_limited` abort and a green Re-check, `run_eligibility` returns `safety_cooldown` with "LinkedIn asked us to slow down — next run allowed after HH:MM" (abort time + 60 min, local), and `POST /runs` is refused with it.
  - At abort + 61 min the run is allowed.
  - After a `checkpoint` (or `logout`) abort the window is 15 min.
  - While `needs_attention` is still set, the reason is `needs_attention`, not `safety_cooldown`.
  - With both values 0 there is no cool-down.
- [ ] After a stop that tripped on keyword K (planned first), K's `last_run_at` equals the stop time, and the next run plans K last. A stop at the run-start check stamps no keyword.
- [ ] With `SALES0_SCRAPE_MAX_RUNS_PER_DAY=1`, a second Run now is refused with `daily_run_cap`.
- [ ] With `SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD=2`, every keyword row reports seen ≤ 2.
- [ ] A Run now while a run is active is refused with `run_in_progress`. Connect during a run is refused with `job_busy`.
- [ ] The Runs page progress panel shows changing counters while the run is active, without a page reload.
- [ ] A server restart during a run → that run shows `failed` "Interrupted by server restart".
- [ ] In live mode, `/__fixtures/linkedin/feed/` returns 404.
- [ ] Every keyword produces a `scrape_keyword` JSON log line, and every run produces a `scrape_run` line.
