# sales0 — Data Model

All timestamps are UTC, timezone-aware. Every table has `id` (integer PK) unless noted. Enum-like fields are lowercase strings with the allowed values listed. Actor-bearing tables carry a nullable `actor_user_id → users.id` for per-user attribution later. The raw text of an **enquiry** is never deleted. The raw text of a post the AI skipped (`seen_posts`, Phase 1.4) is purged after 14 days; its fingerprint and snippet stay.

## Phase 1 entities (migration `0002_phase1_discovery`)

### users
Created empty in Phase 1. Rows arrive in Phase 2/5.
| Field | Type | Notes |
|---|---|---|
| display_name | str(80) | |
| email | str(254) null, unique | |
| created_at | datetime | |

### offering_groups
| Field | Type | Notes |
|---|---|---|
| name | str(60) unique (case-insensitive via `name_key` = lower(trim(name)) unique) | |
| sort_order | int | |
| created_at | datetime | |

### offerings
| Field | Type | Notes |
|---|---|---|
| group_id | FK offering_groups | |
| name | str(80); `name_key` unique | |
| description | str(500) | Shown to the LLM |
| active | bool, default true | Inactive offerings are hidden from triage and from the offering selects |
| sort_order | int | |
| created_at, updated_at | datetime | |

**Seed rows (exact):**
| id | group | name | description |
|---|---|---|---|
| 1 | Training | AI corporate workshops | Instructor-led AI/GenAI awareness and productivity workshops for company teams, managers and leadership, online or on-site. |
| 2 | Training | Technical AI training | Hands-on technical programs for engineers, e.g. "Build your first AI agent", Claude Code, LLM app development. |
| 3 | Services | AI consulting & build services | Consulting, design and build of AI solutions: assistants/chatbots, agents, automation, RAG over company documents. |
| 4 | Products | Products | smallTech's AI products and tools offered to companies. |

Group seeds: Training (1), Services (2), Products (3).

### keywords
| Field | Type | Notes |
|---|---|---|
| text | str(100) | Trimmed, inner whitespace collapsed, original case kept |
| text_key | str(100) unique | lower(text), used for uniqueness and fixture matching |
| active | bool default true | |
| last_run_at | datetime null | |
| created_at | datetime | |
| actor_user_id | FK users null | |

### linkedin_session (singleton, id = 1, seeded)
| Field | Type | Notes |
|---|---|---|
| status | `not_connected` \| `connected` \| `needs_attention` | Seed `not_connected`. "busy" is derived from the job runner and never stored. |
| last_checked_at | datetime null | |
| last_connected_at | datetime null | |
| last_successful_run_at | datetime null | |
| last_error | str(300) null | e.g. "Checkpoint page at /checkpoint/challenge/ — open LinkedIn manually, resolve it, then Connect again" |
| updated_at | datetime | |

### scrape_runs
| Field | Type | Notes |
|---|---|---|
| trigger | `manual` (Phase 4 adds `scheduled`) | |
| mode | `live` \| `fixture` | |
| status | `queued` \| `running` \| `succeeded` \| `aborted` \| `failed` | |
| started_at, finished_at | datetime / null | |
| keywords_planned, keywords_done | int | |
| current_keyword | str null | |
| current_step | str(200) null | Human text, e.g. "Searching 'ai workshop' (2/3) — scrolling", "Triaging 3 new enquiries (1/3)", "Pausing 34s before next keyword" |
| posts_seen, posts_new, posts_dupe | int default 0 | |
| enquiries_triaged, triage_failed, errors_count | int default 0 | |
| abort_reason | `logout` \| `checkpoint` \| `captcha` \| `rate_limited` \| `daily_post_cap` \| null | `daily_post_cap` ends the run as `succeeded`, with the note in current_step. Null on `failed` runs, including fatal LLM errors (`aborted` is reserved for account-safety stops). A fatal LLM error after a safety stop keeps the run `aborted` with its reason (`architecture.md` step 6a). |
| error_message | str(500) null | Also holds the LLM message when a fatal LLM error follows a safety stop. |
| actor_user_id | FK users null | |

### scrape_run_keywords
| Field | Type | Notes |
|---|---|---|
| run_id | FK scrape_runs | |
| keyword_id | FK keywords null (`SET NULL` on delete) | |
| keyword_text | str | Snapshot |
| status | `pending` \| `running` \| `done` \| `warning` \| `error` \| `skipped` | `skipped` = not reached because of an abort or cap. `warning` = a possible LinkedIn layout change. Phase 1.1 uses it for both cases: no posts parsed (drift), and a field missing on at least half of the parsed posts (`architecture.md` data flow step 3). |
| posts_seen, posts_new, posts_dupe, errors | int | |
| duration_ms | int null | |
| error_message | str(300) null | |

### enquiries
The core record: one per real-world RELEVANT post, after dedupe. From Phase 1.4 a row is created only for an AI-`genuine` post (at triage time, already triaged) or by a human Rescue; AI discards live in `seen_posts` (see Phase 1.4 below). Pre-1.4 rows with `triage_status` pending/failed keep the old behaviour.
| Field | Type | Notes |
|---|---|---|
| source | `linkedin_post` (later `whatsapp_group`, `manual`, `import`) | |
| content_hash | str(64) unique | sha256 of `normalize_text(raw_text)` |
| raw_text | text | "Full Enquiry". Kept forever. |
| author_name, author_headline | str null | |
| author_profile_url | str null | |
| source_url | str null | "Link": permalink of the first source; Phase 1.2: else the author profile URL, else the search URL (upgraded when a permalink is found later) |
| date_posted | date null | Estimated from the relative time |
| date_added | datetime | First seen |
| review_status | `pending_review` \| `accepted` \| `discarded` | |
| stage | null \| `prospect_new` | Set to `prospect_new` on accept. Phase 2 moves the lifecycle onto `prospects`. |
| triage_status | `pending` \| `done` \| `failed` | |
| triage_error | str(300) null | |
| triaged_at | datetime null | |
| prompt_version | str null | |
| ai_verdict | `genuine` \| `discard` null | Immutable AI output |
| ai_reason | str(160) null | |
| ai_poster_type | str null | |
| ai_priority | str null | |
| ai_offering_id | FK offerings null | |
| ai_extraction | JSON null | The full guarded `TriageResult` |
| requirement_summary | str(140) null | Editable. Initialised from AI. |
| priority | `high` \| `medium` \| `low` null | Editable |
| offering_id | FK offerings null | Editable |
| company, contact_person, email, phone, city | str null | Editable later (Phase 2 UI). Initialised from AI. |
| mode | `online` \| `offline` \| `hybrid` \| `unknown` null | |
| dates_mentioned, topic | str null | |
| discard_reason | str(200) null | |
| notes | text null | Sheet "Notes" (edited in Phase 2) |
| update_note | text null | Sheet "Update" (Phase 2) |
| date_email_sent, date_proposal_sent | date null | Sheet columns. Written in Phase 2/3. |
| decided_at | datetime null | |
| actor_user_id | FK users null | Last decider |

Indexes: `(review_status, ai_verdict, priority, date_added)`, `date_added`, `company`.

**Sheet → field mapping** (used by Phase 2 import): Date posted→date_posted, Date added→date_added, Requirement Summary→requirement_summary, E-Mail→email, Priority→priority, Status→review_status/stage, Notes→notes, Company→company, Date email sent→date_email_sent, Date proposal sent→date_proposal_sent, Contact number→phone, Link→source_url, Full Enquiry→raw_text, Keyword used→enquiry_keyword_hits, Update→update_note, Contact person→contact_person. Discarded sheet "reason"→discard_reason.

### enquiry_sources
Each distinct source post merged into an enquiry.
| Field | Type | Notes |
|---|---|---|
| enquiry_id | FK enquiries (cascade) | |
| source_ref | str unique | LinkedIn `urn:li:activity:…`, or `ck:<KEY>` when no URN is exposed (Phase 1.2) (later the WhatsApp message id) |
| source_url | str null | |
| first_seen_run_id | FK scrape_runs null | |
| first_seen_at | datetime | |

### enquiry_keyword_hits
| Field | Type | Notes |
|---|---|---|
| enquiry_id | FK enquiries (cascade) | |
| run_id | FK scrape_runs null | |
| keyword_id | FK keywords null (SET NULL) | |
| keyword_text | str | Snapshot |
| seen_at | datetime | |
Unique `(enquiry_id, run_id, keyword_text)`.

### triage_decisions
The learning-loop signal. Append-only.
| Field | Type | Notes |
|---|---|---|
| enquiry_id | FK enquiries | |
| decision | `accept` \| `discard` | |
| ai_verdict | `genuine` \| `discard` null | Snapshot at decision time |
| is_override | bool | True when the decision disagrees with ai_verdict (accept vs discard, discard vs genuine) |
| reason | str(200) null | Discard reason |
| field_overrides | JSON null | e.g. `{"priority": ["low","high"], "offering_id": [2,1]}`, recorded by `update_fields` |
| decided_at | datetime | |
| actor_user_id | FK users null | |

### llm_calls
| Field | Type | Notes |
|---|---|---|
| feature | `ping` \| `triage` (later `research`, `drafting`, `suggest`) | |
| model | str | |
| prompt_version | str null | |
| enquiry_id | FK enquiries null | |
| run_id | FK scrape_runs null | |
| input_tokens, output_tokens | int | |
| cost_usd_micros | int | |
| latency_ms | int | |
| fewshot_count | int default 0 | |
| status | `ok` \| `error` | |
| error_type | str null | |
| request_id | str null | |
| created_at | datetime | indexed |

### status_history
Generic history of every status change.
| Field | Type | Notes |
|---|---|---|
| entity_type | `enquiry` \| `scrape_run` \| `linkedin_session` (Phase 2 adds `prospect`, `lead`, `client`) | |
| entity_id | int | |
| field | str | `review_status`, `stage`, `triage_status`, `status` |
| from_value, to_value | str null | |
| note | str null | |
| changed_at | datetime | |
| actor_user_id | FK users null | |
Index `(entity_type, entity_id, changed_at)`.

### Phase 1.1: no schema change (head stays `0002_phase1_discovery`)
- The **safety cool-down** is derived from `scrape_runs` (`status='aborted'`, `abort_reason`, `finished_at`) by `services.runs.safety_cooldown_until`. Nothing new is stored.
- **`RunView.layout_warnings`** is a count of `scrape_run_keywords.status='warning'`, computed on read.
- The **"just run" stamp** after a safety stop writes the existing `keywords.last_run_at`.
- The **browser channel/label** is resolved at runtime (`scraper.browser.resolve_browser`) and is never stored.
- A run that fails because the profile has no session cookie keeps `abort_reason` null (it is not a safety stop) and sets `linkedin_session.status='not_connected'` with `last_error` "Not logged in to LinkedIn in sales0's browser — press Connect LinkedIn in Setup".
- Future migrations follow `architecture.md` → Conventions → Migrations: never batch-rebuild an FK-referenced table under `foreign_keys=ON`.

### Phase 1.2: no schema change (head stays `0002_phase1_discovery`)
- **`enquiry_sources.source_ref`** may now also be `ck:<KEY>` (LinkedIn's `componentkey` for a search-result post) when the page exposes no URN. A URN learned later (from "Copy link to post") is added as an extra source row for the same enquiry. `String(200)` is enough; a longer KEY is treated as absent by the parser, never truncated.
- **`enquiries.source_url`** ("Link") is the post permalink when known, else the author's profile URL, else the keyword's search URL. A later sighting that has the permalink upgrades it. **`enquiry_sources.source_url`** is the permalink when known, else the search URL the post was seen on.
- **Link kind** (`post | profile | search | other`) is derived from `source_url` on read (`scraper.urls.link_kind_of`); it is never stored.
- **Near-duplicate fingerprint** (same author profile slug + first 300 normalised characters) is computed on read from `author_profile_url` and `raw_text` (`services.dedupe.near_duplicate`); it is never stored. `content_hash` keeps its meaning.
- **`date_posted`** keeps its meaning: estimated from the relative time (`now`, `24m`, `3h`, `2d`, `1w`, `3mo`, `1yr`, `• Edited` ignored).

### Phase 1.4: thorough, idempotent discovery (migration `0003_phase1_4_seen_posts`, new head)

> **Binding storage policy.** A scraped post becomes an `enquiries` row ONLY when the AI verdict is `genuine` (or a human rescues it). Every other scanned post is remembered in `seen_posts` (fingerprint + verdict + reason + snippet), so it is never triaged twice and never duplicated. Authors are remembered in `linkedin_profiles`. Earlier text in this file that says "one enquiry per post" now means "one enquiry per relevant post".

#### linkedin_profiles
The identity Phase 2 prospects and Phase 3 prospect-watch will use.
| Field | Type | Notes |
|---|---|---|
| slug | str(200) unique | Normalised by `scraper.urls.profile_slug`: path `/in/<slug>` or `/company/<slug>` → `in/<slug>` / `company/<slug>`; query, fragment and trailing slash dropped; percent-decoded; lower-cased |
| kind | `person` \| `company` | From the slug prefix |
| profile_url | str(500) | Canonical `https://www.linkedin.com/<slug>/` (live-shaped even in fixture mode) |
| display_name | str(200) null, headline str(500) null | Latest non-null value seen |
| first_seen_at, last_seen_at | datetime | Upserted on EVERY sighting |
| posts_seen | int default 0 | +1 per sighting (scanned card, repeats included) |
| relevant_count | int default 0 | +1 per distinct post that became an enquiry |
| tracked | bool null | NULL = never tracked. Set `true` when a Phase-2 prospect is created from this profile (Phase 2 adds `prospect_id`) |

#### seen_posts
One row per distinct post sighted, whatever the verdict.
| Field | Type | Notes |
|---|---|---|
| fingerprint | str(64) unique | `sha256(author_slug + "\n" + normalize_text(text))`; `author_slug` is `""` when unknown |
| content_hash | str(64) indexed | `dedupe.content_hash(text)` (author-independent; merges the same text posted by two authors, as Phase 1 did) |
| text_prefix | str(300) | `normalize_text(strip_ellipsis(text))[:300]`; survives the purge; feeds the same-author near-duplicate rule |
| source_ref | str(200) unique null | URN or `ck:<KEY>` when present |
| profile_id | FK linkedin_profiles null (SET NULL) | |
| author_name str(200) null, author_headline str(500) null | | |
| keyword_text | str(100) | Keyword of the first sighting |
| first_run_id | FK scrape_runs null (SET NULL) | |
| triage_status | `pending` \| `done` \| `failed` | `failed` = LLM error; retried from the Skipped tab |
| triage_error | str(300) null | |
| verdict | `genuine` \| `discard` null | Immutable AI output (null until triaged) |
| reason | str(160) null, poster_type str(40) null | |
| ai_result | JSON null | The guarded `TriageResult`; used by Rescue |
| snippet | str(240) | First 240 chars of the text, one line. Kept forever |
| raw_text | text null | Full text. NULL once purged, and NULL when the post became an enquiry (the text lives on the enquiry) |
| raw_purge_after | datetime null | `first_seen_at + SALES0_SEEN_RAW_RETENTION_DAYS` (14). Set only while `raw_text` is held |
| raw_purged_at | datetime null | |
| post_url | str(500) null | Permalink when captured, else null |
| posted_relative | str(30) null | As scraped ("3h") |
| enquiry_id | FK enquiries null (SET NULL) | Set when a genuine post, or a rescued one, became an enquiry |
| rescued_at | datetime null | Set by Rescue / Override |
| first_seen_at, last_seen_at | datetime | |
| times_seen | int default 1 | |
Indexes: `(verdict, first_seen_at)`, `profile_id`, `content_hash`, `enquiry_id`.

#### Changes to Phase 1 tables (all in-place `ADD COLUMN`, no table rebuild)
| Table | New columns |
|---|---|
| keywords | `group_name` str(40) null (one of the 4 offering names for suggested keywords); `suggested_key` str(60) null unique (stable slug of the suggested phrase; the seeding identity); `scanned_total`, `relevant_total`, `last_scanned`, `last_relevant` int default 0 (yield stats; `last_*` = the latest run) |
| scrape_runs | `posts_relevant`, `posts_skipped_ai`, `permalinks_found` int default 0 |
| scrape_run_keywords | `posts_relevant`, `posts_skipped_ai`, `permalinks_found`, `scrolls` int default 0; `cost_usd_micros` int default 0 |
| enquiries | `author_profile_id` FK linkedin_profiles null (SET NULL); `seen_post_id` FK seen_posts null (SET NULL); `posted_relative` str(30) null |
| llm_calls | `seen_post_id` FK seen_posts null (SET NULL), indexed. Triage of a new post runs before its enquiry exists; when it becomes an enquiry, its rows also get `enquiry_id` |

Counter meaning (per run and per keyword): `posts_seen` = **scanned** (every parsed card, repeats included); `posts_new` = distinct posts never seen before (each triaged once); `posts_dupe` = already-seen (no LLM call); `posts_relevant` = new posts the AI called genuine (each created an enquiry); `posts_skipped_ai` = new posts the AI discarded; `posts_new = relevant + skipped_ai + triage_failed`. Relevance rate = `posts_relevant / posts_new`.

- **Interrupted runs** (server restart) are `status='aborted'` with `abort_reason` NULL and `error_message` "Interrupted by server restart — the next run continues with the keywords it had not reached" (a new abort reason would need a rebuild of the FK-referenced `scrape_runs`, which the migration rules forbid). They start no cool-down (the cool-down reads only the four safety reasons).
- **Keyword resume**: the next run plans first the active keywords whose row in the most recent run is `pending`/`running`/`skipped` and whose `last_run_at` is null or before that run's `started_at`; then least-recently-run order.
- **Seed**: `app_meta` key `keywords_seeded_v1` = `1` once the suggested library (`capabilities/search-setup.md`) has been auto-seeded at startup.
- **Backfill in 0003** (frozen copies of `normalize_text`, `profile_slug` and the fingerprint formula live inside the migration; a test asserts they equal the app versions):
  1. Every existing enquiry gets a `linkedin_profiles` upsert (when it has an author profile URL) and a `seen_posts` row (`enquiry_id` set, `verdict` = its `ai_verdict`, `raw_text` NULL), and `enquiries.author_profile_id` / `seen_post_id` are set.
  2. Enquiries with `ai_verdict='discard'` AND `review_status='pending_review'` AND no `triage_decisions` row (never touched by a human) are MOVED: their `seen_posts` row keeps `raw_text` (purge after 14 days from the migration time), `enquiry_id` NULL, and the enquiry is deleted (cascading sources, hits; `llm_calls.enquiry_id` becomes NULL and the rows get `seen_post_id`). Costs are preserved.
  3. Everything else stays: accepted enquiries, any enquiry a human decided (including discarded ones, which keep feeding the learning loop and stay in the Discarded tab), `triage_status` pending/failed enquiries (they keep the Phase 1 Retry behaviour), and AI-genuine ones.
  4. Profile counters are recomputed: `posts_seen` = seen_posts rows, `relevant_count` = remaining AI-genuine or accepted enquiries.

### app_meta (scaffold)
`key` str PK, `value` str. Holds `schema_note` and (Phase 1.4) `keywords_seeded_v1`.

### Foreign-key delete rules and key normalisation (binding)
- **Offering references** (`enquiries.offering_id`, `enquiries.ai_offering_id`, `offerings.group_id`): `RESTRICT`. `delete_offering` therefore deletes only an unreferenced offering, and deactivates (`active=false`) one still in use.
- **`triage_decisions.enquiry_id`**: `CASCADE`, so decisions are deleted with their enquiry (as with `enquiry_sources` and `enquiry_keyword_hits`).
- **Run and enquiry links** (`llm_calls.run_id`, `llm_calls.enquiry_id`, `enquiry_sources.first_seen_run_id`, `enquiry_keyword_hits.run_id`): `SET NULL`. `scrape_run_keywords.run_id` is `CASCADE`.
- **`actor_user_id`** on every table: `SET NULL`.
- **`name_key` / `text_key`** are set automatically by the model whenever `name`/`text` is assigned. Callers never set them. `name_key = lower(trim(name))` on offering_groups and offerings. On keywords, `text` is trimmed and inner whitespace collapsed, then `text_key = lower(text)`.

## Lifecycle (binding vocabulary)

```
Enquiry (scraped item)
  review_status: pending_review ──accept──> accepted (stage = prospect_new)
                                ──discard─> discarded (kept, searchable, feeds learning)
  accepted <──> discarded may be re-decided (each decision appended to triage_decisions)

Prospect (Phase 2: company with an open enquiry, OR person shortlisted via outreach)
  stage: new → contacted → follow_up_1 → follow_up_2 → final_follow_up
  exit: converted (→ Lead, only after contact AND a response) | closed

Lead (Phase 2)
  stage: meeting_scheduled → proposal_sent → won | lost | no_response
  won → Client

Client (Phase 2): status active | legacy (older projects, imported)
```

## Phase 2+ entities (summary; full columns are fixed in each phase's migration)

| Entity | Phase | Key fields |
|---|---|---|
| companies | 2 | name, name_key unique, website, linkedin_url, city, is_competitor, notes, research_brief (P3) |
| prospects | 2 | kind (`company_enquiry` \| `outreach_person`), company_id, contact_id null, origin_enquiry_id null, stage, next_follow_up_on date null, closed_reason, actor_user_id. 0004 backfill: one prospect per `accepted` enquiry (stage `new`); each prospect's company/person links to its `linkedin_profiles` row (`tracked = true`) via `enquiries.author_profile_id`. |
| leads | 2 | prospect_id unique, stage, meeting_at, proposal_sent_on, value_estimate null, outcome_note |
| clients | 2 | company_id, status (`active` \| `legacy`), started_on, source (`lead` \| `legacy_import`) |
| contacts | 2 | full_name, email (indexed), phone, linkedin_url (unique null), title, company_id null, tags (via contact_tags) |
| contact_tags / tags | 2 | tag name unique, category (`role`, `segment`, `custom`) |
| contact_links | 2 | contact_id, entity_type (`enquiry` \| `prospect` \| `lead` \| `client` \| `competitor`), entity_id, role |
| timeline_events | 2 | subject_type, subject_id, company_id null, contact_id null, kind (`status_change`, `note`, `email_sent`, `proposal_sent`, `enquiry_linked`, `call`, `meeting`, `research`), body, occurred_at, actor_user_id |
| follow_ups | 2 | prospect_id or lead_id, due_on date, done_at null, note |
| signals | 2 | entity_type, entity_id, kind (`repeat_poster`, `competitor_mention`), detail JSON, created_at |
| competitors | 2 | company_id, approx_clients int null, client_names text, contact, email, website, notes (seed: Future Labs, Savya Technologies, White Spaces, Evolvv, Ritu Arora) |
| channel_groups | 2 | name, platform (`whatsapp`), link, details (`keep` \| `keep_low` \| free text), active |
| import_batches | 2 | filename, sheet_kind (`active` \| `discarded` \| `prospects` \| `competitors` \| `groups`), rows_total, rows_new, rows_merged, rows_error, created_at |
| outreach_templates / decks / email_drafts / sent_emails | 3 | per-offering template (subject, body with placeholders); deck PDF path + offering_id/group_id; draft (prospect_id, version, subject, body, deck_id, status `draft` \| `approved` \| `sent` \| `failed`, approved_by, approved_at); gmail message/thread ids |
| profile_candidates | 3 | name, linkedin_url unique, company, headline, reason_post_url, status (`message_sent` \| `connection_request_sent` \| `closed` \| `new`), notes |
| weekly_targets | 4 | metric_key, target (seed: linkedin_scrape 15, whatsapp_scrape 5, linkedin_outreach 5, messages_sent 25, first_followups 20) |
| schedules | 4 | kind `discovery_run`, cron-like times (≤3/day), enabled |
| digests | 4 | for_date, body, suggestions JSON |
| users (populated) | 5 | password_hash, role |
