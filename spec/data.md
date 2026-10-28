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

**Sheet → field mapping** (historical, Phase 1 draft; **superseded by `## Phase 2 entities` → "Sheet record → CRM column mapping"**, which is binding for the read-only sheet sync and the CSV import): Date posted→date_posted, Date added→date_added, Requirement Summary→requirement_summary, E-Mail→email, Priority→priority, Status→review_status/stage, Notes→notes, Company→company, Date email sent→date_email_sent, Date proposal sent→date_proposal_sent, Contact number→phone, Link→source_url, Full Enquiry→raw_text, Keyword used→enquiry_keyword_hits, Update→update_note, Contact person→contact_person. Discarded sheet "reason"→discard_reason.

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

Prospect (Phase 2: ONE per linkedin_profile; or per enquiry / sheet row / manual entry when there is no profile)
  stage: new → contacted → follow_up_1 → follow_up_2 → final_follow_up      (prospects.stage)
  exit (prospects.status): open → converted (→ Lead, only after contact AND a response) | closed (closed_reason)

Lead (Phase 2)
  stage: meeting_scheduled → proposal_sent → won | lost | no_response       (leads.stage; won/lost/no_response set closed_at)
  won → Client (active)

Client (Phase 2): status active | legacy (older projects, imported or typed in)
```

Full column definitions of every Phase 2 table, the sheet-owned vs CRM-owned split and the sheet-status mapping follow in `## Phase 2 entities` below.

## Phase 2 entities (ONE migration: `migrations/versions/p6_crm_5b2e7d14.py`, `down_revision = "p5_replies_c3d81f20"`)

> **Binding conventions for every Phase 2 table.** Timestamps are UTC `UTCDateTime`; `created_at`/`updated_at` on every table unless noted. Enum-like columns are lowercase strings validated by a CHECK (new tables only; CHECKs are never added to an existing, FK-referenced table). Actor attribution is `actor` (str(80): a team member's name, `system`, `sheet-sync`, `import`) plus `actor_member_id` FK `team_members` `SET NULL`; the Phase 1 `actor_user_id` (users) is NOT used by Phase 2 tables. All `*_key` columns are `lower(trim(...))` with inner whitespace collapsed, set by the service layer. A table's "sheet columns" are the five defined under `### Sheet-fed tables`.
>
> The shared Python enum tuples live in `src/sales0/db/models/enums_crm.py`; the model modules are `db/models/{crm,contacts,timeline,registries,sheetdata}.py` (see `architecture.md` → Phase 2 layout).

### companies
| Field | Type | Notes |
|---|---|---|
| name | str(200) | |
| name_key | str(200) unique | |
| website | str(300) null, website_key str(200) null indexed | `sheetsync.values.website_key` form |
| linkedin_url | str(500) null, city str(100) null, notes text null | |

### contacts (shared people; built for 10k+ rows)
| Field | Type | Notes |
|---|---|---|
| full_name | str(200) null | at least one of full_name / an email / linkedin_slug is required |
| email | str(254) null | display/primary; lower-cased. ALL addresses also live in `contact_emails` |
| phone | str(40) null | display/primary, `+<digits>` |
| linkedin_slug | str(200) null **unique** | `scraper.urls.profile_slug` form (`in/<slug>`); the matching key |
| linkedin_url | str(500) null, title str(200) null, notes text null | |
| company_id | FK companies null, `SET NULL`, indexed | |
| search_text | str(700) | `lower(full_name + ' ' + email + ' ' + company name)`; rebuilt on every write; the search column (`LIKE %q%`) |
| merged_into_id | FK contacts null (`SET NULL`) | set on the dropped row's timeline only; the dropped row itself is deleted, so this stays NULL in practice (reserved) |
| origin | `manual` \| `enquiry` \| `sheet` \| `import` \| `reply` | first source |
| actor, actor_member_id | | |
Indexes: `ix_contacts_company_id`, `ix_contacts_full_name` (`full_name` COLLATE NOCASE), unique `linkedin_slug`.
**contact_emails**: `contact_id` FK `CASCADE`, `email` str(254) **unique** (lower-cased), `is_primary` bool. **contact_phones**: `contact_id` FK `CASCADE`, `phone` str(40), `digits` str(20) (last 10 digits; indexed), unique `(contact_id, digits)`.
**tags**: `name` str(40), `name_key` unique, `category` `role` \| `segment` \| `custom` (default `custom`). **contact_tags**: composite PK `(contact_id, tag_id)`, both `CASCADE`. Index `tag_id`.
**contact_links**: `contact_id` FK `CASCADE`, `entity_type` (`enquiry` \| `prospect` \| `lead` \| `client` \| `competitor` \| `company`), `entity_id` int (no FK; polymorphic), `role` str(40) null (`author`, `buyer`, `recipient`, `replied`, `sheet`), unique `(contact_id, entity_type, entity_id)`, index `(entity_type, entity_id)`.

### prospects
One per `linkedin_profiles` row (unique), else one per origin enquiry / sheet row / manual entry.
| Field | Type | Notes |
|---|---|---|
| kind | `company_enquiry` \| `outreach_person` | enquiry/manual-company = `company_enquiry`; Outreach/Prospects/Working rows = `outreach_person` |
| name | str(200) | display name: person, else company |
| linkedin_profile_id | FK linkedin_profiles null **unique**, `RESTRICT` | the identity; NULL when there is no profile (SQLite allows many NULLs) |
| company_id | FK companies null (`SET NULL`), indexed | |
| contact_id | FK contacts null (`SET NULL`), indexed | primary contact |
| origin_enquiry_id | FK enquiries null (`SET NULL`) | the first enquiry (CRM-owned link) |
| stage | `new` \| `contacted` \| `follow_up_1` \| `follow_up_2` \| `final_follow_up` | default `new` |
| stage_source | `app` \| `sheet` | who last set the stage; the sync NEVER overwrites `app` |
| status | `open` \| `converted` \| `closed` | default `open`; `converted` once a lead exists |
| closed_reason | str(60) null, closed_at datetime null | `no_response`, `lost`, `not_interested`, `enquiry_discarded`, `unsubscribed`, `sheet: <raw status>`, free text |
| contacted_at | datetime null, contacted_estimated bool default false | `estimated` = derived from a sheet status, not an observed send |
| responded_at | datetime null | "response received" (reply or manual). Required with contacted_at for a lead |
| next_follow_up_on | date null | denormalised: earliest open `follow_ups.due_on` (maintained by `followups`) |
| source | `enquiry` \| `sheet` \| `manual` \| `import` | |
| reason_url, reason_text, extra_link | str(1000) / str(500) / str(1000) null | sheet "Reason of adding" (post link or text), the spare column |
| notes | text null | CRM-owned free text (timeline notes are separate rows) |
| actor, actor_member_id | | |
| *sheet columns* | | see below |
Indexes: `(status, stage)`, `next_follow_up_on`, `company_id`, `contact_id`, unique `linkedin_profile_id`.

### leads
| Field | Type | Notes |
|---|---|---|
| prospect_id | FK prospects `CASCADE`, indexed | partial unique index `uq_leads_one_open_per_prospect (prospect_id) WHERE closed_at IS NULL` |
| company_id | FK companies null `SET NULL` | copied from the prospect |
| stage | `meeting_scheduled` \| `proposal_sent` \| `won` \| `lost` \| `no_response` | |
| stage_source | `app` \| `sheet` | as on prospects |
| opened_at | datetime | |
| closed_at | datetime null | set by won/lost/no_response; cleared if moved back |
| meeting_at | datetime null, proposal_sent_on date null, value_estimate int null (whole INR), outcome_note str(500) null | |
| opened_by | `reply` \| `manual` \| `sheet` | |
| actor, actor_member_id | | |
Index `(stage, closed_at)`.

### clients
`name` str(200); `company_id` FK null `SET NULL`; `status` `active` \| `legacy`; `source` `lead` \| `legacy_import` \| `manual` \| `sheet`; `lead_id` FK leads null unique `SET NULL`; `prospect_id` FK prospects null `SET NULL`; `started_on` date null; `notes` text null; `actor`, `actor_member_id`. Index `(status)`.

### follow_ups (reminders)
| Field | Type | Notes |
|---|---|---|
| prospect_id / lead_id | FK `CASCADE` null | exactly one is set (CHECK) |
| kind | `follow_up_1` \| `follow_up_2` \| `final_follow_up` \| `custom` | |
| due_on | date | local-calendar date |
| note | str(300) null, auto bool | `auto` = scheduled by the system |
| done_at, done_actor, cancelled_at, cancel_reason | datetime / str / datetime / str(80) null | open = both `done_at` and `cancelled_at` NULL |
Index `(due_on, done_at, cancelled_at)`, `prospect_id`, `lead_id`.
**Schedule rule (config in Settings):** when a prospect is observed `contacted` at `T` (the app saw a send, or a human moved it) the service creates three open reminders: `follow_up_1` at `T + SALES0_FOLLOWUP_DAYS_1` (3), `follow_up_2` at `T + SALES0_FOLLOWUP_DAYS_2` (7), `final_follow_up` at `T + SALES0_FOLLOWUP_DAYS_FINAL` (14), calendar days from the contact date (not from the previous follow-up). Completing a reminder moves the prospect to that reminder's stage (and completes earlier open ones); moving the prospect to a later stage completes the reminders up to it. A recorded response, a lead, a close, or the `no_follow_up` flag cancels the open ones. Sheet-derived stages and the migration backfill create NO reminders.

### flags
| Field | Type | Notes |
|---|---|---|
| entity_type | `prospect` \| `enquiry` \| `contact` \| `company` \| `client` \| `lead` | |
| entity_id | int | no FK (polymorphic) |
| kind | `repeat_poster` \| `competitor_mention` \| `no_follow_up` | |
| source | `auto` \| `user` | `auto` flags are recomputed; `user` flags are never auto-cleared |
| detail | JSON null | `{"count": 3, "within_days": 60}` / `{"competitor_ids": [1], "names": ["Future Labs"]}` / `{"reason": "unsubscribe", "reply_id": 12}` |
| cleared_at, cleared_by | datetime / str null | cleared flags are kept for history |
Partial unique index `uq_flags_active (entity_type, entity_id, kind) WHERE cleared_at IS NULL`. Index `(kind, cleared_at)`.

### timeline_events (append-only; every change to every record)
| Field | Type | Notes |
|---|---|---|
| subject_type / subject_id | `prospect` \| `lead` \| `client` \| `contact` \| `company` \| `enquiry` \| `competitor` \| `group` + int | the record the event is about |
| prospect_id, enquiry_id, company_id, contact_id | FK null (`SET NULL`), each indexed | the aggregation scopes: a lead/client/email event also carries its `prospect_id`; an enquiry event carries its `prospect_id` and `company_id`. A page's timeline = `WHERE <scope>_id = X` |
| kind | `created` \| `stage_change` \| `note` \| `email_sent` \| `reply_received` \| `lead_created` \| `lead_stage` \| `client_created` \| `proposal_sent` \| `enquiry_linked` \| `flag_set` \| `flag_cleared` \| `follow_up_added` \| `follow_up_done` \| `follow_up_cancelled` \| `tracked_changed` \| `contact_merged` \| `contact_linked` \| `call` \| `meeting` \| `research` \| `import` \| `sheet_created` \| `sheet_updated` \| `sheet_removed` \| `sheet_restored` \| `sheet_status` | |
| body | text null | human sentence; for `sheet_*` events it lists changed FIELD NAMES only, never cell values (personal data) |
| data | JSON null | e.g. `{"from": "new", "to": "contacted"}`, `{"message_id": 4}`, `{"reply_id": 9, "intent": "interested"}` |
| actor, actor_member_id | | `sheet-sync` for every sheet-driven event |
| occurred_at | datetime indexed | |
Indexes: `(prospect_id, occurred_at)`, `(company_id, occurred_at)`, `(contact_id, occurred_at)`, `(enquiry_id, occurred_at)`, `(subject_type, subject_id, occurred_at)`. The Phase 1 `status_history` rows of an enquiry are merged into the read model (`timeline.list_timeline`), not copied.

### competitors
`name` str(200), `name_key` unique; `website` str(300) null; `website_key` str(200) **unique null**; `approx_clients` int null; `client_names` text null; `contact` str(300) null; `email` str(254) null; `emails` JSON null (every address of the sheet cell); `notes` text null (CRM-owned); `active` bool default true; `company_id` FK null; *sheet columns*. **Seeded in the migration (idempotent by `name_key`, `origin` not sheet):** Future Labs, Savya Technologies, White Spaces, Evolvv, Ritu Arora.
**competitor_mentions**: `competitor_id` FK `CASCADE`, `entity_type` (`enquiry` \| `client` \| `prospect`), `entity_id`, `source` `auto` \| `user`, `snippet` str(200) null, `created_at`; unique `(competitor_id, entity_type, entity_id)`, index `(entity_type, entity_id)`. This is how "competitors link to enquiries/clients" is stored; the `competitor_mention` flag on the entity is derived from it.

### channel_groups
`name` str(200), `name_key`; `platform` str(20) (`whatsapp` default, `linkedin`, `telegram`, `facebook`, `other`; lower-cased); `link` str(500) null, `link_key` str(500) **unique null**; `details` str(200) null (raw sheet text); `keep_level` `keep` \| `keep_low` \| null; `active` bool default true (Phase 5 ingests only active groups); `notes` text null; *sheet columns*. Unique `(platform, name_key)` when `link_key` is NULL is enforced by the service.

### import_batches (app-owned CSV import / paste; NOT sheet sync)
`filename` str(255); `kind` `enquiries` \| `discarded` \| `outreach` \| `prospects` \| `working` \| `competitors` \| `groups` \| `clients` \| `contacts` \| `paste`; `rows_total`, `rows_new`, `rows_merged`, `rows_unchanged`, `rows_error` ints; `errors` JSON (`[{"row": 7, "code": "bad-date:date_posted"}]`, capped at 500); `actor`, `actor_member_id`; `created_at`.

### Sheet-fed tables: sheet columns and the sheet-owned / CRM-owned contract
Applies to `enquiries`, `prospects`, `competitors`, `channel_groups` (all added to the existing tables with plain `ADD COLUMN`). Columns:
| Column | Type | Meaning |
|---|---|---|
| `sheet_status` | str(200) null | the raw sheet Status cell, never remapped |
| `sheet_notes` | text null | the raw sheet Notes cell (the CRM's own `notes` is separate) |
| `sheet_removed_at` | datetime null | set (soft) when the row disappears from every tab that fed it; cleared on return. **Never a hard delete.** |
| `sheet_fields` | JSON null | the merged overlay of every tab's `record.sheet_fields()` (audit + the "previous value" for clear detection); not rendered except in the "Synced from sheet" panel |
| `origin` (enquiries only) | `discovery` \| `sheet` \| `manual` \| `import`, default `discovery` | the enquiries `source` CHECK stays `linkedin_post` (rebuilding the referenced table is forbidden); `origin` says who created the row |

**`sheet_sources`** (per entity, per tab; the `sheet_source` of the requirements): `entity` str(12) (`enquiry` \| `discarded` \| `prospect` \| `competitor` \| `group` \| `metric`; the `SheetRecord.ENTITY`), `key` str(200) (the record's natural key), `target_type` (`enquiry` \| `prospect` \| `competitor` \| `channel_group` \| `sheet_metric`), `target_id` int, `sheet_id` str(64), `gid` str(20), `tab_name` str(60), `row_number` int, `row_hash` str(40), `fields` JSON (this tab's `sheet_fields()` snapshot, used for the overlay and to detect cleared cells), `first_seen_at`, `last_seen_at`. Unique `(entity, key, sheet_id, gid)`. Indexes `(sheet_id, gid)` (the `list_keys_for_tab` query) and `(target_type, target_id)` (the "Synced from sheet" panel). A removed row's source is deleted (`mark_removed` drops that tab's source); the target gets `sheet_removed_at` when no live source remains.

**Ownership** (binding; mirrors the `services/sheetsync/repository.py` docstring):
- SHEET-OWNED = exactly the columns in the mapping table below. `upsert` overwrites them; a new non-null value wins; a cell that was non-null in the stored `fields` and is now null clears the column; null→null leaves it alone. Sheet wins every conflict.
- CRM-OWNED, never touched by a sync: `timeline_events`, every `ai_*` column and `triage_decisions`, `notes` / `update_note` (on non-sheet rows), `flags`, `follow_ups`, `tags`, `contact_links`, `stage` / `status` / `closed_*` / `contacted_at` / `responded_at` of prospects and leads once `stage_source = 'app'`, `review_status` of a non-sheet enquiry, `raw_text` and `date_added` of a discovery-origin enquiry, `llm_calls`, `outreach_*`.
- A sheet row that matches an existing app record (same post, same profile, same website) ADOPTS it: a `sheet_sources` row is added and only sheet-owned columns are written.
- The app can edit CRM-owned fields of a sheet-fed row freely. The UI marks sheet-owned fields "Synced from sheet" (edits would be overwritten), and `DELETE`-style actions on a sheet-fed competitor/group return `sheet_owned` (remove the row in the sheet instead).

### sheet_metrics (baseline for the Phase 4 dashboard)
| Table | Columns |
|---|---|
| `sheet_metrics` | `metric_set` `weekly` \| `secondary`; `group_label` str(200) null (Secondary section label carried down); `metric` str(200); `metric_key` str(300) **unique** (= the record key, e.g. `weekly:/leads#2`); `weekly_target` float null; `weekly_target_raw` str(60) null; `sheet_removed_at`; `sheet_fields` JSON; `updated_at` |
| `sheet_metric_weeks` | `metric_id` FK `CASCADE`; `week_no` int (1-53); `occurrence` int (1; 2+ for a duplicated "Week 43" column); `label` str(20); `raw` str(60); `value` float null; unique `(metric_id, week_no, occurrence)` |
The sheet gives no year: `week_no` is stored as is. Phase 4 assigns the ISO year by "nearest past occurrence" when it merges these with computed figures. `sheet_metrics` rows are replaced wholesale (the weeks are deleted and re-inserted) when the row hash changes. No timeline events (there is no timeline subject).

### Changes to existing tables (in the same migration)
| Table | Change | How |
|---|---|---|
| enquiries | `prospect_id` FK prospects null `SET NULL` (indexed); `company_id` FK companies null `SET NULL`; `contact_id` FK contacts null `SET NULL`; `origin`, `sheet_status`, `sheet_notes`, `sheet_removed_at`, `sheet_fields` | plain `ALTER TABLE enquiries ADD COLUMN ... [REFERENCES ... ON DELETE SET NULL]` via `op.execute` (as 0003 did); NO table rebuild (enquiries is FK-referenced) |
| prospects / competitors / channel_groups | (new tables, sheet columns included) | `create_table` |
| outreach_messages | FKs on the existing plain columns: `prospect_id` → prospects, `enquiry_id` → enquiries, `company_id` → companies, all `ON DELETE SET NULL` | needs a table rebuild and `outreach_messages` IS referenced (`outreach_events`, `outreach_replies`, itself) → the **four-step recipe** in `architecture.md` Migrations: inside `with op.get_context().autocommit_block():` run `PRAGMA foreign_keys=OFF`, `batch_alter_table("outreach_messages", recreate="always", reflect_args=[...restate follow_up_of_id FK ON DELETE SET NULL...])` with the three `create_foreign_key`, then `PRAGMA foreign_key_check` (must return no rows, else raise), then `PRAGMA foreign_keys=ON`. Before the rebuild: `UPDATE outreach_messages SET prospect_id = NULL, company_id = NULL` and `SET enquiry_id = NULL WHERE enquiry_id NOT IN (SELECT id FROM enquiries)` (the Phase 3 ids pointed nowhere). After the backfill, `prospect_id`/`company_id` are re-derived from `enquiry_id`. |
| company_briefs | FKs `prospect_id`, `enquiry_id` (leaf table → plain batch) | `batch_alter_table` |
| linkedin_profiles | none (the prospect holds the FK; `tracked` is set by the service/backfill) | |
| status_history | `entity_type` is a free string already; Phase 2 stages are written to `timeline_events`, not `status_history` | |

### Backfill in the migration (frozen helpers inside the migration; a test asserts they equal the app versions)
1. `companies`: one per distinct `lower(trim(company))` of enquiries with `review_status='accepted'`.
2. `contacts`: one per distinct lower-cased `email` among accepted enquiries; else one per `(lower(contact_person), company_key)` when a person is named; `contact_emails`/`contact_phones`/`contact_links(enquiry, role=author|buyer)` rows.
3. `prospects`: one per accepted enquiry **grouped by `author_profile_id`** when present, otherwise per enquiry. Stage `new`; but `contacted` (with `contacted_at` = `date_email_sent` or the first `outreach_messages.sent_at` of a message linked to it, `contacted_estimated` = true only for the date-only case) when either exists. `kind='company_enquiry'`, `source='enquiry'`, `origin_enquiry_id` = the oldest enquiry. `enquiries.prospect_id/company_id/contact_id` are set, `linkedin_profiles.tracked = 1` for each profile that got a prospect.
4. `timeline_events`: one `created` per prospect (`actor='backfill'`, `occurred_at` = the enquiry's `decided_at`), one `enquiry_linked` per additional enquiry, one `email_sent` per `sent` outreach message that resolves to a prospect.
5. `outreach_messages.prospect_id/company_id` re-derived from `enquiry_id`.
6. The five competitors are seeded. NO follow-up reminders, NO flags (flags are recomputed once at first startup: `app_meta.crm_flags_backfilled_v1`).
Row counts of every pre-existing table are asserted unchanged after the upgrade (migration test), and `PRAGMA foreign_key_check` returns no rows.

### Sheet status → CRM stage mapping (binding; implemented in `services/sheetsync/mapping.py` + `status_map.py`)
**Binding rule: a Lead exists only after contact AND a response.** The raw text stays in `sheet_status`. The mapped stage is applied **only when `stage_source != 'app'`** (a human's move always wins; the sync then refreshes `sheet_status` and writes a `sheet_status` timeline event only when the status would actually have changed something, once per distinct value). The sync creates NO reminders. Matching is **exact-phrase on a normalised cell** (casefolded, punctuation stripped, tokenised, whitespace collapsed) against the whitelists below; there is NO substring matching. A **negation guard** applies first: a cell containing any of `no`, `not`, `yet`, `to`, `be`, `pending`, `awaiting`, `unsent`, `without`, `never`, `soon`, `will`, `need`, `needs`, `draft`, `planned` (as a token) never matches a contacted / proposal / meeting phrase (`Message not sent`, `To be sent`, `Consent pending`, `Not yet sent`, `unsent`, `No proposal yet`, `Proposal to be sent`, `Not interested in a proposal` all fall through). Rules are evaluated top to bottom, first match wins. "contact evidence" = `date_email_sent` or `date_proposal_sent` present. **Unknown statuses** (non-blank, matching nothing) map to rule 9 (`new`), are logged once per distinct value as a `sheet_status` timeline event, and are counted per tab as `unknown_statuses` in the sync report (not an error).

**Active Enquiries rows** (record `SheetEnquiry`) always create/adopt an accepted enquiry (`origin='sheet'` if new, `review_status='accepted'`, `stage='prospect_new'`) and a prospect, then:
| # | Sheet Status (or date) | Prospect | Lead |
|---|---|---|---|
| 1 | `won`, `closed won`, `converted`, `client`, `confirmed`, `project done`, `completed`, `delivered` | `converted` (stage kept at its last observed value, else `contacted`) | `won` → active **client** (`source='sheet'`) |
| 2 | `lost`, `closed lost`, `rejected`, `declined`, `not interested`, `dropped` and contact evidence | `converted` | `lost` |
| 2b | `closed` / `close` AND `date_proposal_sent` present | `converted` | `lost` |
| 3 | `no response`, `no reply`, `not responding`, `ghosted` | contact evidence: `converted`, else `closed` (`closed_reason='no_response'`) | contact evidence: `no_response` lead whose prospect gets **no** `responded_at`; else none |
| 4 | `meeting scheduled`, `meeting`, `call scheduled`, `interested`, `replied`, `responded` (ONLY these) | `converted` | `meeting_scheduled` |
| 5 | `proposal sent`, `sent proposal`, `proposal submitted`, `proposal shared`, `quotation sent`, `quote sent`, OR `date_proposal_sent` present (a proposal date implies a response; any status not matched above) | `converted` | `proposal_sent` (`proposal_sent_on` = the date) |
| 6 | `follow up 1` / `followup 1` / `fu1` / `first follow up` | `follow_up_1` | none |
| 6b | `follow up 2` / `followup 2` / `fu2` / `second follow up` | `follow_up_2` | none |
| 6c | `final follow up` / `last follow up` | `final_follow_up` | none |
| 7 | `contacted`, `message sent`, `email sent`, `mail sent`, `emailed`, `messaged`, `connection request sent`, `sent`, OR `date_email_sent` present | `contacted` (`contacted_at` = `date_email_sent` else first sync, `contacted_estimated`) | none |
| 8 | `closed`, `close`, `inactive`, `hold`, `on hold`, `parked`, `cancelled`, `na` | `closed`, `closed_reason = "sheet: <raw>"` | none |
| 9 | blank, `new`, `open`, `pending`, `to do`, temperature words (`hot`, `warm`, `in progress`, `ongoing` - they do NOT create a lead), negated phrases (above), anything else | `new` | none |
A lead created by rules 1-5 gets `opened_by='sheet'`, `opened_at` = `date_email_sent` or `date_added` or the first sync, `responded_at` = `opened_at` and `contacted_estimated` set (rules 1-5 except rule 3 `no_response`, which sets no `responded_at`: the sheet moving a row to those states IS the evidence of contact and response, and a proposal date implies a response; the app-level guard applies only to app-driven conversion), and for closed outcomes `closed_at` = `date_proposal_sent` or the first sync. A later sheet edit that changes the mapped outcome while `stage_source='sheet'` updates the lead stage in place (timeline `lead_stage`, actor `sheet-sync`). **Unknown statuses never abort a row.**

**Outreach / Prospects / Working Sheet rows** (`SheetOutreachPerson`, `SheetProspect`) create/adopt a prospect (`kind='outreach_person'`, `source='sheet'`); same normalisation and negation guard:
| Sheet Status | Prospect |
|---|---|
| `Message sent`, `Connection request sent`, `Contacted`, `Email sent`, `Mail sent`, `Sent` (exact phrase) | `contacted` (`contacted_estimated`) |
| `Closed` | `closed`, `closed_reason='sheet: Closed'` |
| blank, `new`, `open`, `pending`, `to do`, `replied`, `responded`, negated phrases (`Message not sent`, `To be sent`, `Consent pending`, `Not yet sent`, `unsent`), anything else | `new` (anything not listed and not a negated `sent` phrase is counted as unknown) |

**Mass-removal and header guards** (`spec/capabilities/sheet-sync.md`): a tab is refused (`removal_blocked` + an error entry) when its fetched text has a header but zero usable data rows while live rows exist, or when more than 50% of >= 3 live rows vanish; a header that does not look like the tab's kind (e.g. Google served the first tab for a wrong gid) is an unusable tab (`header-mismatch`) and is never ingested.
**Discarded Enquiries rows** (`SheetDiscarded`): an `enquiries` row with `review_status='discarded'`, `discard_reason` = Notes (else the Status text), `origin='sheet'`, `triage_status='done'`, `ai_*` NULL; Status `No Response` / `Rejected` is kept in `sheet_status` (not mapped to a prospect). **No `triage_decisions` row is written** (the sheet's discard labels are not AI-relevance labels; the Phase 1.5 calibration found them ambiguous). If an Active row and a Discarded row resolve to the same enquiry, the Active row wins (accepted); the Discarded reason is only stored in `discard_reason`.

### Sheet record → CRM column mapping (every `SheetRecord` field; "SO" = sheet-owned, written by the sync)
| Record field | Target | Notes |
|---|---|---|
| **Enquiry / Discarded** `key`, `key_source` | `sheet_sources.key`, `.fields` | bookkeeping |
| `date_posted` | `enquiries.date_posted` SO | |
| `date_added` | `enquiries.date_added` SO **only for `origin='sheet'`** (local midnight → UTC); else `sheet_fields` only | |
| `requirement_summary` | `enquiries.requirement_summary` SO (clip 140) | |
| `summary_company`, `summary_topic`, `summary_city`, `engagement_mode` | company name fallback; `enquiries.topic` SO; `enquiries.city` SO; `enquiries.mode` SO | `company` cell wins over `summary_company` |
| `emails` | `enquiries.email` SO (first); every address → `contacts` / `contact_emails` | |
| `email_note` | `sheet_fields` only | UI: "e-mail cell had no valid address" |
| `priority` | `enquiries.priority` SO (only `high\|medium\|low`) | |
| `sheet_status`, `sheet_notes` | `enquiries.sheet_status`, `.sheet_notes` SO | |
| `company` | `enquiries.company` SO + `companies` row | |
| `date_email_sent`, `date_proposal_sent` | `enquiries.date_email_sent`, `.date_proposal_sent` SO | feed the status rules |
| `phones` | `enquiries.phone` SO (first); all → `contact_phones` | |
| `contact_link` | `contacts.linkedin_url` / `linkedin_slug` if it is a LinkedIn profile; else `sheet_fields` | |
| `post_url` | `enquiries.source_url` SO | also the adoption key |
| `link_text` | `sheet_fields` only | |
| `full_enquiry` | `enquiries.raw_text` SO **only for `origin='sheet'`** (else fallback chain: summary, `"(no text in the sheet row)"`); `content_hash` = sha256 of normalised text, or of `"sheet:"+key` when there is none | |
| `keyword_used` | `sheet_fields` only | |
| `sheet_update` | `enquiries.update_note` SO for `origin='sheet'` rows | |
| `contact_person` | `enquiries.contact_person` SO + `contacts.full_name` | |
| `discard_reason` (Discarded) | `enquiries.discard_reason` SO (clip 200) | |
| **Person** `key`, `key_source`, `linkedin_slug` | bookkeeping | |
| `name` | `prospects.name` SO + `contacts.full_name` | |
| `linkedin_url` | `linkedin_profiles` (`profiles.ensure_profile`) → `prospects.linkedin_profile_id`; `contacts.linkedin_slug/url` | an existing prospect of that profile is ADOPTED (never a second one) |
| `company`, `website` | `companies` row (`website` fills `companies.website` when empty) | |
| `sheet_status`, `sheet_notes` | `prospects.sheet_status`, `.sheet_notes` SO | |
| `reason_url`, `reason_text`, `extra_link` | `prospects.reason_url`, `.reason_text`, `.extra_link` SO | |
| `source_tab` | `sheet_sources.tab_name` (shown as chips "Outreach", "Prospects", "Working Sheet") | |
| **Competitor** `website`, `website_key`, `name`, `approx_clients`, `client_names`, `contact`, `emails`, `sheet_notes` | `competitors.website`, `.website_key`, `.name`, `.approx_clients`, `.client_names`, `.contact`, `.email`+`.emails`, `.sheet_notes` — all SO | adoption by `website_key`, else `name_key` (so the 5 seeded competitors are adopted, not duplicated) |
| **Group** `name`, `platform`, `link`, `details`, `keep_level` | `channel_groups.name`, `.platform` (null → `whatsapp`), `.link`/`.link_key`, `.details`, `.keep_level` (`keep-low` → `keep_low`) — all SO | adoption by `link_key`, else `(platform, name_key)` |
| **Metric** `metric_set`, `metric`, `group`, `weekly_target(_raw)`, `weeks` | `sheet_metrics` / `sheet_metric_weeks` | |

**No row lost without an error entry (binding invariant, tested per tab):** every non-blank data row of a tab ends in exactly one of: `added`, `updated`, `unchanged`, or an entry in the tab's `errors` list (parser errors such as `no-identifying-content`, `row-parse-failed`, `duplicate-row`; or `apply-failed:<ExceptionName>` when the repository could not apply a parsed row). A duplicate natural key that differs is re-keyed by row hash (warning `duplicate-key-rekeyed`) and still applied. So `tab.rows == added + updated + unchanged + len({e.row for e in errors})` for a tab that did not fail to fetch. Blank and template-placeholder rows are counted in `blank_rows` and are not data rows. An `apply-failed` row rolls back its own transaction only; the tab continues; the stored row hash is NOT updated so the next sync retries it.

### Later-phase entities (built; defined by their models and migrations)

| Entity | Phase | Key fields |
|---|---|---|
| outreach_templates / decks / email_drafts / sent_emails | 3 | per-offering template (subject, body with placeholders); deck PDF path + offering_id/group_id; draft (prospect_id, version, subject, body, deck_id, status `draft` \| `approved` \| `sent` \| `failed`, approved_by, approved_at); gmail message/thread ids |
| profile_candidates | 3 | name, linkedin_url unique, company, headline, reason_post_url, status (`message_sent` \| `connection_request_sent` \| `closed` \| `new`), notes |
| weekly_targets | 4 | metric_key, target (seed: linkedin_scrape 15, whatsapp_scrape 5, linkedin_outreach 5, messages_sent 25, first_followups 20) |
| schedules | 4 | kind `discovery_run`, cron-like times (≤3/day), enabled |
| digests | 4 | for_date, body, suggestions JSON |
| users (populated) | 5 | password_hash, role |
