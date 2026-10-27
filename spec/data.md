# sales0 — Data Model

All timestamps are UTC, timezone-aware. Every table has `id` (integer PK) unless noted. Enum-like fields are lowercase strings with the allowed values listed. Actor-bearing tables carry a nullable `actor_user_id → users.id` for per-user attribution later. Raw scraped text is never deleted.

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
| abort_reason | `logout` \| `checkpoint` \| `captcha` \| `rate_limited` \| `daily_post_cap` \| null | `daily_post_cap` ends the run as `succeeded`, with the note in current_step |
| error_message | str(500) null | |
| actor_user_id | FK users null | |

### scrape_run_keywords
| Field | Type | Notes |
|---|---|---|
| run_id | FK scrape_runs | |
| keyword_id | FK keywords null (`SET NULL` on delete) | |
| keyword_text | str | Snapshot |
| status | `pending` \| `running` \| `done` \| `warning` \| `error` \| `skipped` | `skipped` = not reached because of an abort or cap |
| posts_seen, posts_new, posts_dupe, errors | int | |
| duration_ms | int null | |
| error_message | str(300) null | |

### enquiries
The core record: one per real-world post, after dedupe.
| Field | Type | Notes |
|---|---|---|
| source | `linkedin_post` (later `whatsapp_group`, `manual`, `import`) | |
| content_hash | str(64) unique | sha256 of `normalize_text(raw_text)` |
| raw_text | text | "Full Enquiry". Kept forever. |
| author_name, author_headline | str null | |
| author_profile_url | str null | |
| source_url | str null | "Link": permalink of the first source |
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
| source_ref | str unique | LinkedIn `urn:li:activity:…` (later the WhatsApp message id) |
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

### app_meta (scaffold)
`key` str PK, `value` str. Holds `schema_note`.

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
| prospects | 2 | kind (`company_enquiry` \| `outreach_person`), company_id, contact_id null, origin_enquiry_id null, stage, next_follow_up_on date null, closed_reason, actor_user_id. 0003 backfill: one prospect per `accepted` enquiry (stage `new`). |
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
