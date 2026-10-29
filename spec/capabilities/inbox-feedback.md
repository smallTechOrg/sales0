# Inbox feedback (2026-10-29): pipeline repeats, real post links, editable triage criteria

Migration `q14_criteria_5b8e1f3a` (down_revision `p11_rawcapture_a4d9c7e2`). Team feedback, three items.

## A1. Repeat enquiries from prospects already in the pipeline

- **Check** (`services/crm/pipeline_match.py`, called from `seen_posts._create_enquiry` for every new enquiry): first hit
  wins: author LinkedIn profile -> prospect; author profile slug -> known contact -> prospect; post email -> contact
  email -> prospect; author is a company page -> `companies.linkedin_url`; AI company name -> `companies.name_key`;
  non-free-mail email domain -> `companies.website_key`. A company hit uses its newest live prospect, else the bare
  company. Prospects closed only as `enquiry_discarded` do not count. No LLM call.
- **Mark**: `enquiries.pipeline_prospect_id / pipeline_company_id / pipeline_match / pipeline_run_id` (plain ids).
  The card shows "Already in pipeline: <prospect>" linking `/pipeline/prospects/{id}` (or `/companies/{id}`).
- **Rule (per scrape run)**: the first matching enquiry per prospect (or bare company) stays in To review; each further
  one in the SAME run is discarded by the system: `review_status='discarded'`, `discard_source='auto'`,
  `discard_reason` "Repeat from existing prospect <name> (already N this run)", a status_history row, and a timeline
  `note` on the prospect (company when no prospect) with `data.auto_discard='repeat'`. No `triage_decisions` row, so
  it never feeds the few-shot examples. Outside a run (rescue, retry triage, paste) it only marks.
- **Run summary**: `scrape_runs.posts_repeat_skipped` (not counted as relevant; no permalink click is spent on them);
  "Repeats from pipeline auto-skipped N" on the run summary and "· repeats from pipeline skipped N" in the step text.
- **Accept**: `prospects.ensure_for_enquiry` joins the MARKED prospect (after profile identity and origin lookups)
  instead of creating a duplicate. A team decision on an auto-discarded card clears `discard_source`.
- **Sheet**: sheet rows never pass this path. A sheet row may still decide an auto-discarded discovery enquiry
  (`mapping.apply_record`: `may_decide` also when `discard_source='auto'`). Watch: auto-discarded repeats are not
  `TrackedProfileHit`s.

## A2. Proper post links (fallback: post -> profile -> search)

- Order everywhere: post permalink -> author profile -> only if neither, the search page:
  `post_from_raw`, `dedupe._source_url` (enquiry_sources), `seen_posts._create_enquiry` (enquiry + its source).
- `scraper.parsing.card_post_urn`: reads the post URN off what the card capture already holds (`raw.card.links`
  same-site hrefs + `outer_html`), no click and no page load; only when exactly one post id is present (one activity
  id, else one ugcPost/share id) and never for a reshare. The link improves; the identity (`source_ref`) does not
  change. "Copy link to post" capture (cap 15/keyword, 3 misses) is unchanged.
- UI: a search link is never presented as the post. Card: search only when no profile is known, labelled "Search
  results (post and profile links unavailable)". Prospect / lead / competitor pages use `link_label(url)`: "View post"
  / "View profile" / "Search results (no post link)" / "Open link".
- Backfill (in the migration): an enquiry whose `source_url` is a search page gets the author profile URL (enquiry
  author URL, its `linkedin_profiles` row, or the seen post's `raw_card.author_profile_url`); an enquiry source with a
  search URL gets the enquiry's post/profile link. Unknown authors keep the search link.

## A3. Transparent, editable triage criteria + discard reasons

- Tables: `triage_criteria` (kind include|exclude, text <= 600, position, active, source seed|team|discard,
  from_enquiry_id, created/updated by + at) and `triage_criteria_sets` (immutable snapshot of the ACTIVE rules per
  change; `version` + 1). Seeded with the 13 rules hard-coded in triage-v3 (version 1). `llm_calls.criteria_version`
  traces each triage call to its rule set.
- Prompt: `prompts/triage_system.md` keeps the fixed scaffolding; the two numbered lists are rendered from
  `TriageDeps.criteria` (`services.criteria.active_set`, used by discovery, hosted worker, retry, `triage_enquiry`).
  With the seed the rendered system prompt is byte-identical to triage-v3, so `PROMPT_VERSION` stays `triage-v3` and
  the cached system block stays stable until a rule changes. `criteria=None` (eval, unit tests) = built-in defaults.
- Page `/criteria` (nav "Criteria", after Setup): include and exclude lists (order = prompt order), add / edit (text,
  list) / enable-disable / move up-down, recent versions, and "Why posts were discarded": Team reasons (latest
  decision per still-discarded enquiry, grouped by reason text), System (auto repeats), AI (skipped seen posts by
  poster type), each with 30-day and all-time counts and up to 3 example posts. Everyone can view; changes are
  admin-only (`require_admin`, 403 `forbidden`; controls hidden for members).
- Discard form (card and bulk bar): preset select + free-text "Or your own reason" (`custom_reason`, <= 200 chars,
  wins over the preset; stored as `triage_decisions.reason`, so it reaches the few-shot examples) + admin-only "Save
  as a triage rule (exclude)" (`save_rule=1`, creates a `source='discard'` exclude rule; an existing identical rule is
  re-enabled or reported).
