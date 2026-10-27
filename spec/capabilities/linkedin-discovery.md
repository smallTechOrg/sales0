# Capability: LinkedIn Discovery (Connect, Run now, Dedupe, Run Log, Safety)
**Phase 1, reworked in Phase 1.1 (Real-Chrome connect + robust LinkedIn detection) and Phase 1.2 (LinkedIn's new search-results layout)**

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
| Enquiries | one per distinct post (raw text, author, link, date, keywords). The link is the post itself when sales0 could get it, otherwise the author's profile (labelled as such) | Triage → Review inbox |
| Live progress | current keyword/step, counters | Runs page (refreshes every second) |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| LinkedIn (team session, sales0's own Google Chrome profile on this computer) | open feed (session check), content search results by keyword, scroll, expand "see more", and (Phase 1.2) open a post's "…" menu and press "Copy link to post" | Each of these makes sales0 **stop the run immediately**, close the browser, set "needs attention" with the reason, show the banner and never retry automatically: logout (sent to a login page or the authwall, or the session cookie disappears), a checkpoint/challenge page, a captcha, or a rate limit (HTTP 429/999 on any LinkedIn response, including background requests the page makes). It checks before each keyword and after every page load, scroll, "see more" and post-menu action. Other page errors: that keyword is marked `error` and the run continues with the next keyword. |
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
- **Phase 1.2 — LinkedIn's new search layout.** LinkedIn's search results no longer carry post ids or post links. sales0 recognises posts by LinkedIn's stable page-structure labels (post, author block, post text, "…more", post menu), and still recognises the older id-based layout when LinkedIn serves it. Which layout was read is logged per keyword.
  - A post without a LinkedIn id is identified by LinkedIn's per-result key. Because that key may change between visits, duplicates are ALSO merged by normalised text (as before) and by "same author + same opening ~300 characters" (so a post read once truncated and once in full is one enquiry). The same post under two keywords or in two runs is one enquiry.
  - **Post link.** For posts without a link, sales0 opens the post's "…" menu and presses **"Copy link to post"**, slowly and with a safety check after each step. It intercepts the copy inside the page, so the computer's clipboard is never touched, and it never presses any other menu item. At most N posts per keyword are tried (default 15; 0 turns it off), and it gives up on a keyword after 3 failures in a row. When the link can't be had, the card links to the **author's profile** and says "post link unavailable". A link found in a later run replaces the profile link.
  - The post time ("now", "24m", "3h", "2d", "1w", "3mo", "1yr", with or without "• Edited") becomes the approximate date posted.
  - A results page that shows result items but yields no readable posts is a layout warning (as before). Posts whose text can't be read on at least half of ≥ 2 results are a layout warning "couldn't read post text on M".
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

**Phase 1.2 (HOW: `architecture.md` → `## LinkedIn SDUI search results`)**
- [ ] The sanitised saved SDUI sample parses (real headless browser) to exactly 6 posts with `strategy=sdui`, the 6 pinned `ck:` refs in DOM order, and the pinned author, profile URL, headline, relative time and text snippet per post; `drift=false`.
- [ ] The sanitisation test passes: every `/in/` and `/company/` slug, author name, `aria-label`/`alt` and componentkey in the sample is from the synthetic list; no script, iframe, `licdn.com`, member id, tracking attribute, or attribute outside the allowlist.
- [ ] The legacy saved sample (`search_results_content.html`) still parses to its 6 URN posts with `strategy=urn`; fixture `legacy-urn-test` returns 10 posts with their URNs and 0 permalink attempts.
- [ ] Fixture run 1 / run 2 counts hold with no URN anywhere in the fixture DOM: 9/7/2/7, then 11/2/9/2. P1 has both keyword chips and 2 `ck:` source rows; P2 has 2 source rows (its URN + P7's `ck:` ref); exactly 7 enquiries after run 1.
- [ ] Permalink capture on fixture `ai workshop` (default cap): P5's card links "View post ↗" to `{base}/feed/update/<P5's corpus URN>/` (no `utm_`/`rcm` query) and P5's source ref is that URN; P1 and P6 link "Author profile (post link unavailable) ↗" to their `/in/<slug>/`; `window.__fixture_menu_clicks` contains only "Copy link to post"; `scrape_keyword` logs `strategy: "sdui"`, `permalink_attempts: 3`, `permalinks_found: 1`.
- [ ] Clipboard isolation: on a page whose original `navigator.clipboard.writeText` is wrapped to record calls, after `CLIPBOARD_HOOK_JS` and a "Copy link to post" click, the URL is captured and the original was called 0 times.
- [ ] `SALES0_SCRAPE_PERMALINK_MAX_PER_KEYWORD=0` → 0 menu opens; every SDUI card links to the author profile; counts unchanged. `=1` on `ai workshop` → exactly 1 menu open.
- [ ] 3 consecutive capture misses stop capture for that keyword (fixture `permalink-miss-test`, 4 posts whose menus all write nothing, gets exactly 3 menu opens).
- [ ] `near_duplicate`: same author slug and the first 120 normalised chars of a truncated text equal the expanded text's → True; different author → False; overlap < 100 chars → False. `upsert_post` merges such a pair as `dupe_content` and upgrades a profile link to a permalink when the later sighting has one.
- [ ] `parse_relative` maps `now`, `24m`, `3h` → today; `2d` → today−2; `1w` → today−7; `3mo` → today−90; `1yr` → today−365; `2d • Edited •` → today−2.
- [ ] `layout-drift-test` (SDUI listitems, no posts) → `strategy=none`, `cards_found=3`, keyword `warning`. A page where ≥ half of ≥ 2 SDUI containers have no text → `warning` "couldn't read post text on M".
