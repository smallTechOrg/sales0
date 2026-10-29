# sales0 — AI-Native Design

**Conclusion: AI augments the product** (outcome 2 of the lens). sales0 is a conventional CRUD + scraping app. Its core value comes from specific AI capabilities:

- Phase 1: enquiry triage, with a learning loop.
- Phase 3: company research and outreach drafting.
- Phase 4: digest and suggestions.

Each capability is a separately composed, bounded agent behind the `LLMClient` layer. Every one uses the configured Haiku model only (`SALES0_LLM_MODEL`, default `claude-haiku-5-5`, the cheapest model available on this key; see `architecture.md` → Stack for prices and rationale), and every call is logged with tokens and cost. No sampling or thinking parameters are sent (`architecture.md` → LLM layer contract, call-shape rule). Provider access is the Anthropic API key (`SALES0_ANTHROPIC_API_KEY`) plus, for org-level keys that are not workspace-scoped (this user's key), `SALES0_ANTHROPIC_WORKSPACE_ID`, sent as the `anthropic-workspace-id` header. A missing workspace id surfaces as a fatal `LLMAuthError` naming the variable (`architecture.md` → LLM layer contract).

## Lens evaluation (catalogue in `agentic-ai.md`)

| # | Pattern | Decision | Rationale |
|---|---|---|---|
| 1 | Prompt chaining | **Used, in a degenerate form (one structured call + repair edge)** | Triage is a fixed transform (post → verdict + extraction) with all context in-prompt and no tools to call. The catalogue allows going below the ReAct floor for exactly this case. One batched call keeps cost minimal (user constraint). |
| 2 | Routing | Rejected | All enquiries share one prompt. Poster-type routing would add calls without improving accuracy. |
| 3 | Parallelization | **Used from Phase 1.4 (bounded: 4)** | Phase 1.4 scans ≥ 200 posts a run, ~100+ of them new, so sequential triage (2–5 s each) would add ~10 min of idle browser. A thread pool of `SALES0_TRIAGE_CONCURRENCY` (4) over independent, DB-free `run_triage` calls; persistence stays on the job thread. Bounded so Anthropic 429s stay rare (the adapter still retries). |
| 4 | Reflection | Rejected | It doubles cost. The human reviewer *is* the critic (HITL), and overrides feed learning. |
| 5 | Tool use | **Used in Phase 3 (research)**; forced tool use is also the structured-output mechanism for triage | Research needs live data (company website, LinkedIn company page). |
| 6 | Planning | Rejected | No multi-step goals. Research is bounded to ≤4 steps. |
| 7 | Multi-agent | Rejected | One agent per feature suffices. Splitting multiplies Haiku calls. |
| 8 | Memory management | **Used: long-term DB memory** | Decisions (Phase 1) and the timeline (Phase 2) are the memory. Few-shot retrieval reads decisions, and drafting (Phase 3) reads the timeline. No vector store is needed at low-thousands scale. |
| 9 | Learning & adaptation | **Used (Phase 1)** | The team's accept, discard and override decisions are the feedback signal, re-injected as few-shot examples. |
| 10 | MCP | Rejected | The tools are in-process (fetch, DB), so a protocol adds overhead. |
| 11 | Goal setting & monitoring | Deferred to Phase 4, deterministic | Weekly targets (15/5/5/25/20) are tracked by plain SQL in the metrics dashboard, not by an LLM. |
| 12 | Exception handling & recovery | **Used** | Retry/backoff in the adapter, a schema-repair edge in triage, and a `failed` status with a Retry button. Fatal auth or model errors stop the run with a clear message. |
| 13 | Human-in-the-loop | **Used (core)** | Every AI verdict is reviewed in the inbox. Phase 3: a human approves every email send. |
| 14 | RAG | Rejected | The offerings catalogue (≤ ~20 items) fits in-prompt. Few-shot selection is recency/override-based, not embedding-based. |
| 15 | A2A | Rejected | Single process. |
| 16 | Resource-aware optimisation | **Used** | Haiku only. One call per enquiry. Inputs truncated (`SALES0_TRIAGE_MAX_INPUT_CHARS`), few-shot capped (`SALES0_FEWSHOT_MAX_EXAMPLES`, `…_TEXT_CHARS`). Duplicates and already-seen posts (`seen_posts`) are never re-triaged. Output capped at 900 tokens (`SALES0_LLM_MAX_OUTPUT_TOKENS`). |
| 17 | Reasoning techniques | **ReAct in Phase 3 research**; no chain-of-thought in triage | A one-line reason is the only visible output. Hidden reasoning tokens would cost money for no UI gain. |
| 18 | Guardrails | **Used** | JSON-schema tool input, Pydantic validation, and anti-hallucination guards (see below). LLM text is escaped in the UI. |
| 19 | Evaluation & monitoring | **Used** | `llm_calls` table plus `llm_call` log lines. The fixture corpus is the regression eval, run in the gate against real Haiku. The override rate (decisions where `is_override`) is stored and becomes a metric in Phase 4. |
| 20 | Prioritization | **Used (light)** | The LLM assigns priority high/medium/low by explicit rules. The inbox sorts by verdict, then priority, then recency. |
| 21 | Exploration & discovery | Phase 4 (keyword suggestions) | It proposes new keywords and groups from accepted-enquiry language. A human adds them. |
| 22 | LLM code execution | Rejected | There are no open-ended data questions. Metrics are fixed queries. |

## Capability 1 — Enquiry triage (Phase 1, wired in full)

**Pattern composition:** a single structured call (#1) with forced tool use as the output schema (#5 mechanism), guardrails (#18), a repair edge (#12), few-shot learning from human decisions (#9, #8), HITL review (#13), and per-call instrumentation (#19, #16).

### State (`agent/state.py`)

```python
@dataclass
class TriageState:
    inp: TriageInput
    offerings: list[OfferingRef]
    examples: list[DecisionExample]          # already selected + truncated
    prompt_version: str                      # "triage-v1" (constant in nodes.py; bump on prompt edits)
    request: StructuredRequest | None = None
    raw_tool_input: dict | None = None
    result: TriageResult | None = None
    validation_error: str | None = None
    attempts: int = 0                        # LLM requests made (max 2)
    responses: list[LLMResponse] = field(default_factory=list)
    error: str | None = None
```

### Nodes (`agent/nodes.py`) — each is `def node(state, deps) -> TriageState`

1. **build_prompt** renders `prompts/triage_system.md`. The system prompt contains the role, smallTech's business, the decision rules, the priority rules, the output-field rules, static seed examples (3 in Phase 1; 10 from Phase 1.4, see Prompt v2), and the active offerings list as `id | name | group | description`. It then renders `prompts/triage_user.md.j2`: the few-shot block, then the enquiry (author, headline, date, url, text truncated to `SALES0_TRIAGE_MAX_INPUT_CHARS`). Every untrusted string placed inside the prompt's delimiters (few-shot example text and reasons, and all enquiry fields) is delimiter-escaped first: `<` → `&lt;`, `>` → `&gt;`, so post text cannot open or close a delimiter. It builds a `StructuredRequest(feature="triage", tool_name="record_triage", input_schema=TriageResult JSON schema, max_tokens=SALES0_LLM_MAX_OUTPUT_TOKENS, prompt_version, log_context={enquiry_id, fewshot_count, input_summary})`.
2. **call_model** runs `deps.llm.structured(state.request)`. It appends the response and increments `attempts`, then sets `raw_tool_input`. A transient failure after the adapter's retries → `state.error = "LLM unavailable: <type>"`. A rejected request (non-auth, non-404 4xx; `architecture.md` → LLM layer contract) → `state.error = "AI request rejected — see logs"` (the enquiry is `failed` with Retry; the run continues). `LLMAuthError` and `LLMModelNotFound` propagate (fatal).
3. **validate_and_guard** parses `raw_tool_input` into `TriageResult`. On a ValidationError, or when there is no tool_use block, it sets `validation_error` (a compact message: field paths and why). On success it applies the guards from `agent/guards.py`:
   - `email`: lower-cased. It is set to null unless the string appears case-insensitively in the raw text as a whole address (anti-hallucination). The match is boundary-aware: it must not be immediately preceded by an email character (`[a-z0-9._%+-]`) nor followed by `[a-z0-9-]` or by `.` plus an alphanumeric, so `a@b.co` does not match inside `xa@b.com`, while a trailing sentence period is allowed.
   - `phone`: set to null unless its digit sequence (≥8 digits, ignoring spaces, dashes and `+`) appears in the raw text's digit stream.
   - `offering_id`: set to null unless it is one of the provided active offering ids.
   - `reason`: whitespace collapsed to one line and truncated to 160 chars (ellipsis). If empty, it falls back to `"No reason given"`.
   - `requirement_summary`: one line, ≤140 chars. If empty, it is built as `"{company or author or 'Unknown'} - {topic or 'AI requirement'} - {city or mode}"`.
   - `company`, `contact_person`, `city`, `topic`, `dates_mentioned`: stripped. Empty strings become null.
   - `language`: lower-cased, at most 8 chars; empty becomes `"und"`.
   - Consistency: `verdict == "discard"` forces `priority = "low"`.
4. **repair** (only when `validation_error` and `attempts < 2`) appends the assistant's tool_use and a user message: "Your record_triage input was invalid: {validation_error}. Call record_triage again with corrected fields." Then it routes back to call_model.
5. **finalize** returns `TriageOutcome(status="done", result, llm_responses=responses, fewshot_count=len(examples), prompt_version)`. If `error` is set, or `validation_error` persists after 2 attempts, the status is `"failed"` with `error` set to a human message ("AI returned an invalid answer twice", or "LLM unavailable — retry later").

### Edges / assembly (`agent/graph.py`)

```python
def run_triage(inp, deps) -> TriageOutcome:
    s = TriageState(inp, deps.offerings, deps.examples, PROMPT_VERSION)
    s = build_prompt(s, deps)
    while True:
        s = call_model(s, deps)                     # fatal errors propagate
        if s.error: break
        s = validate_and_guard(s, deps)
        if s.result or s.attempts >= 2: break
        s = repair(s, deps)
    return finalize(s, deps)
```

**Error handler:** the adapter handles transient retries (`SALES0_LLM_MAX_RETRIES`, exponential backoff with jitter). Graph-level failures produce `status="failed"`: the enquiry is saved with `triage_status="failed"` and `triage_error`, the inbox shows it with a **Retry triage** button, and the run continues. Fatal `LLMAuthError`/`LLMModelNotFound` (including the workspace-required case) makes `discovery_job` stop: the run ends `failed` with the adapter's message, remaining keywords are `skipped`, scraped enquiries stay `pending` (Retry triage later), the `llm_calls` error row is kept, and `abort_reason` stays null (see `architecture.md` → Discovery run data flow, step 6a). Scraping that already finished is kept.

**Concurrency (Phase 1.4):** `agent/batch.py::triage_concurrently` runs up to `SALES0_TRIAGE_CONCURRENCY` (default 4) `run_triage` calls at once in a thread pool and yields results in completion order. Workers touch no DB session; the job thread persists each result as it arrives. The first fatal `LLMAuthError`/`LLMModelNotFound` stops submissions (unstarted items stay `pending`) and fails the run as before. The same `AnthropicClient` is shared (it is thread-safe). `retry_triage` from the UI still runs synchronously in the request (one call, ~2–5 s, with a spinner). It is refused with `job_busy` while a run is triaging that same post. Haiku 5.5 only; no sampling or thinking parameters; per-call cost tracking unchanged.

**Subject (Phase 1.4):** a new post is triaged BEFORE it is an enquiry. `TriageInput.subject="seen_post"` carries the `seen_posts` id (`log_context` key `seen_post_id`). `services.seen_posts.persist_triage` writes the `llm_calls` rows (`seen_post_id`) and, only for `genuine`, creates the enquiry (all fields initialised from the result, then `llm_calls.enquiry_id` is back-filled) and the `status_history` rows. A discard is stored in `seen_posts` only (verdict, reason, poster_type, `ai_result`, snippet). Re-sighting a seen post never calls the LLM.

**Prompt v2 (`prompts/triage_system.md`, `PROMPT_VERSION = "triage-v2"`).** Keyword results are a mix of buyers and noise, so the prompt is built from the team's sheets, not from topics:
- The first rule: a keyword is a topic, not intent. A post that merely discusses AI training, agentic AI, a launch, a webinar, a conference, a course, a certification, a hiring drive or an opinion is `discard` (`poster_type=other`) unless it asks for a provider.
- `genuine` = a company, institute, HR/L&D, CTO/engineering or business person (or someone writing for one) seeking a training/workshop/consulting/build PARTNER or an AI product from a provider like smallTech. A named contact route (email, phone, "DM me", "reach out") raises priority; its absence lowers it to medium but does not make it a discard.
- `discard` = agencies, marketplaces and edtech companies (edureka, upGrad, Briltus-style) recruiting or empanelling freelance trainers for THEIR clients; individuals/recruiters seeking one freelance trainer at hourly/day rates; trainers advertising themselves; event/webinar/course promotions; full-time or contract job posts; thought leadership, news and celebrations; requests outside the offerings.
- Built-in examples grow from 3 to 10 and are synthetic (fictional companies, `.example` emails): 3 genuine (L&D head wanting a workshop partner with email; CTO wanting Claude Code training for developers with no contact route; COO wanting an AI assistant built), 7 discard (edtech empanelling trainers; marketplace "trainer requirement" for its clients; recruiter wanting one trainer hourly; trainer advertising; event promo; full-time job post; thought-leadership post about "agentic AI training"). Learned few-shot examples still follow them.
- Hallucination guard unchanged (null unless literally in the text); `anti-injection` paragraph unchanged.

**Eval (Phase 1.4).** A labelled synthetic set (≥ 40 posts, no real people) in `tests/integration/agent/eval_set.py` is the regression for the prompt: the real-Haiku test asserts ≥ 90 % verdict accuracy and 0 hallucinated emails/phones. A prompt change that drops below it does not ship.

**Finalize / persistence for enquiries triaged the old way (`services/triage.py`, retry of legacy pending/failed enquiries):** it writes the `ai_*` fields, initialises the editable fields (`priority`, `offering_id`, `company`, `contact_person`, `email`, `phone`, `city`, `mode`, `requirement_summary`) from the result, and sets `triage_status`, `triaged_at`, `prompt_version`. It inserts one `llm_calls` row per response (feature `triage`, `enquiry_id`, `run_id`, tokens, `cost_usd_micros`, `latency_ms`, `fewshot_count`, `status`) and one `status_history` row (`triage: pending → done|failed`).

### Tool schema (`record_triage` input = `TriageResult`)

Fields and the meaning given in the system prompt:

- `verdict` is `genuine` when the poster is a company or team that wants to BUY training, a workshop, consulting/build or a product from a provider like smallTech. It is `discard` for: training agencies or marketplaces recruiting freelance trainers; individuals or recruiters seeking an individual trainer at hourly or per-day rates; trainers advertising themselves or open to work; requests far outside smallTech's offerings ("too specific", e.g. SAP ABAP); and non-requirements.
- `reason`: ONE line, ≤160 chars, starting with the decisive fact (e.g. "Agency recruiting freelance trainers for its clients, not a buyer").
- `poster_type`: `buyer_company` | `training_agency` | `individual_trainer_seeker` | `job_seeker_or_self_promo` | `other`.
- `requirement_summary`: format "Company - Topic - City/Mode" (e.g. "Acme Logistics - GenAI workshop for 40 managers - Pune/Offline").
- `priority`: `high` = genuine, clear corporate need, contact or route available, near-term; `medium` = genuine but vague, distant or with no contact route; `low` = marginal or discard.
- `offering_id`: the best-matching id from the provided catalogue, or null.
- `company`, `contact_person`, `email`, `phone`, `city`: exactly as written in the post, or null. Never guessed.
- `mode`: `online` | `offline` | `hybrid` | `unknown`.
- `dates_mentioned`: free text, or null. `topic`: short, or null. `language`: ISO 639-1 of the post's main language (`en`, `hi`, …).

### Few-shot selection — the learning loop (`agent/fewshot.py`)

- `load_candidates(db, exclude_enquiry_id, limit=200)`: the latest decision per enquiry (by `decided_at`) from `triage_decisions` joined with `enquiries.raw_text`, newest first, excluding the current enquiry. It is read-only.
- `select_examples(candidates, max_n, text_chars)` is pure and deterministic:
  1. Take overrides first (`is_override=True`), newest first, up to `ceil(max_n/2)`.
  2. Fill the remaining slots alternating discard/accept (starting with whichever kind is less represented so far), newest first.
  3. If one kind runs out, fill from the other.
  4. Truncate each text to `text_chars` (on a word boundary, with an ellipsis).
  5. Return them in chronological order (oldest first).
- In the prompt, each example is rendered as: `Post: <text>`, then `Team decision: ACCEPT (genuine)` or `DISCARD — reason: <reason>`, plus `(AI originally said <x>; the team overrode it)` for overrides.
- With 0 candidates (cold start), only the 3 static seed examples in the system prompt are used, and `fewshot_count=0`.
- The cost bound: ≤6 × 600 chars ≈ ≤1k extra input tokens per call (≈$0.0001 at the default $0.10 / $0.50 per MTok, `architecture.md` → Stack). A typical triage call (~2.5k input + ~300 output tokens) costs ≈$0.0004, so 150 posts a day comes to ≈$0.06. These prompts stay far below the 100K-token threshold where the higher price tier starts.

### Observability

Every request produces an `llm_call` JSON log line (fields in `architecture.md` → Conventions) and an `llm_calls` row. The run detail page shows the cost per run, each inbox card shows its cost, and the header shows the totals for today and this month.

## Capability 2 — Company research (Phase 3)

**Composition:** a ReAct loop (#17 + #5) with guardrails (#18) and exception handling (#12), and a hard budget (#16).

- **State:** `ResearchState(company_name, hints: website?, linkedin_url?, steps: list[Step], findings: dict, brief: str | None, error)`.
- **Tools:**
  - `fetch_website(url)`: httpx GET with a 10s timeout, ≤200 KB, text extracted, ≤6k chars returned.
  - `linkedin_company_page(slug)`: through the Phase-1 browser layer, counting against the daily caps, with safety stops honoured. It is skipped if the session is not connected.
  - `record_brief(summary, industry, size_hint, ai_maturity_hint, talking_points[≤3], sources[])`: the terminal tool.
- **Edges:** reason → tool → observe, for at most 4 tool calls. The loop finalizes when `record_brief` is called, or forces `record_brief` with `tool_choice` on step 4.
- **Error handling:** a tool error is passed back as the observation text. An LLM fatal error fails the research row. A partial brief is allowed.
- **Finalize:** the brief is stored on the company record and as a timeline event, and the `llm_calls` rows have `feature="research"`.

**q15 (prospect research, `capabilities/prospect-research.md`, `PROMPT_VERSION = "research-v2"`):** the same loop now also receives `<our_records>` (CRM data, trusted, quoted third-party text is data) and `<external_data>` sources: the LinkedIn company page text uploaded by the laptop worker (a separate one-page `company_page` worker task with the discovery safety machinery, replacing the in-request `linkedin_company_page` tool) and web-search findings. Web search is a separate single request before the loop (`research-web-v1`): server tool `web_search_20250305` (`max_uses` 3) + `record_web_findings` with `tool_choice=auto`; findings must cite a URL the search returned. The user turn is a cached stable head + a per-step tail. Results are stored as versions in `prospect_docs`.

## Capability 2b — Account summary (q15)

**Composition:** a single structured call (#1) with forced tool use (`record_account_summary`, `summary-v1`, feature `summary`), over long-term DB memory (#8: enquiries, contacts, research, notes, outreach, replies, follow-ups, timeline, sheet), guardrails (#18: only facts from the records, third-party text is data) and HITL (#13: editable, every edit a version). No repair call (cost); an unusable answer is a failed version the user can retry. Drafting (`draft-v2`) reads the newest usable summary as background.

## Capability 3 — Outreach drafting (Phase 3)

**Composition:** a single structured call (#1) over a per-offering template, using the company brief and timeline as context (#8), with HITL approval (#13) and guardrails (#18).

The tool is `record_draft(subject, body_markdown, deck_id | null)`. Guards: it must not invent prices, it must not exceed 180 words, and it must contain no unfilled `{{placeholders}}`. The output is a draft only. Sending requires an explicit human Approve on that exact version. Any edit after approval requires approval again.

## Capability 4 — Digest + suggestions (Phase 4)

**Composition:** a single structured call (#1, #21). Deterministic SQL computes yesterday's numbers and today's follow-ups. Haiku writes a 5-bullet digest and proposes ≤5 new keywords, derived from the language of recently accepted enquiries that is absent from the active keywords. The suggestions are shown with Add/Ignore buttons. Nothing is auto-added.

## Phase 5

WhatsApp ingestion reuses Capability 1 unchanged: `source="whatsapp_group"`, with the same dedupe and the same triage. Reply tracking is deterministic (Gmail thread polling), with no LLM.


## Inbox feedback - editable criteria (prompt stays `triage-v3`)
The include / exclude rule lists of the system prompt come from `triage_criteria` (`TriageDeps.criteria`, a
`CriteriaSet`; `None` = the built-in seed). The rest of the prompt is fixed. `TriageOutcome.criteria_version` ->
`llm_calls.criteria_version`. System auto-discards (pipeline repeats) write no `triage_decisions`, so few-shot stays
human-only; free-text discard reasons are stored as the decision reason and rendered in the examples as before.
See `capabilities/inbox-feedback.md`.

## Phase 1.5 - recall-first triage (`PROMPT_VERSION = "triage-v3"`)

Evidence: a live mini-run scanned 94 buyer-intent posts and discarded all of them, because rule 1 discarded every agency / edtech "trainer requirement". The team's own Active Enquiries sheet shows the opposite practice: it responds when a company or institute (even via an agency) asks a training PROVIDER / partner / organisation to deliver a programme.

- New field `opportunity_type` (`direct_buyer | agency_requirement | individual_trainer_wanted | other`), stored in `enquiries.ai_opportunity_type` (migration `0004_opportunity_type`, backfilled from `ai_extraction`). `poster_type` is unchanged. The inbox card shows a chip (not for `other`); within a priority, `direct_buyer` sorts first.
- Verdict rules: `genuine` for direct buyers, for agency/edtech/recruiter posts with a CONCRETE requirement (client or programme, topic, dates/city, batch) that invite providers / trainers / organisations to respond, for RFP / vendor-empanelment / provider-pool calls, and for concrete freelance-trainer requirements for a corporate programme (priority `low`). `discard` only for payroll job posts, a single cheap hourly tutor, trainer/company self-promotion, event/course promotion, thought leadership/news/spam, trainer-network building with no concrete requirement, and clearly out-of-scope topics.
- Few-shot examples are synthetic rewrites (A-N). Hallucination guards are unchanged.
- Ground-truth eval: `uv run python -m sales0.cli eval-triage` fetches the real Active / Discarded sheets as public CSV at runtime (never stored), labels Active rows `genuine`, Discarded rows `discard` except "No Response"/"Rejected"/"not replying" (genuine-but-lost), runs real Haiku, and prints confusion counts, recall, discard-correct rate, and miss row numbers with scrubbed one-line reasons (no post text or contact data). Targets: recall >= 0.85 on Active rows, discard-correct >= 0.75. Discarded rows without `Full Enquiry` text fall back to the one-line Requirement Summary (low fidelity). `tests/eval/` wraps it under the `eval` marker (excluded from the default run).
- Search diversification: `scraper/variants.py` plans up to `SALES0_SCRAPE_MAX_PASSES_PER_KEYWORD` passes per phrase (baseline date-filter passes, then a daily-rotating mix of other sort, quote-wrapped, #hashtag, `urgent`/`requirement`/`enquiry` suffixes, `past-24h`); live mode only.
- Permalinks: LinkedIn now copies `https://lnkd.in/p/<code>`; `permalink.resolve_short_link` follows that single redirect anonymously (HEAD, no cookies) and stores `/feed/update/urn:li:activity:N/`.

## Feedback round 2 - category suggestion (`PROMPT_VERSION = "triage-v4"`)
`record_triage` gained `category` (`relevant_prospect` | `channel_partner` | `competitor`; default and fallback
`relevant_prospect`), explained in a static `## Category` section of `triage_system.md` with two more built-in examples (O
channel partner, P competitor). It is a SUGGESTION in the same call (no extra LLM call per post): stored as
`enquiries.ai_category` and copied to the editable `category`; it changes neither verdict nor priority. The section is part
of the fixed scaffold (before the rules, independent of the post), so the system block stays identical across posts and
prompt caching is unaffected; the rules still come from the DB criteria page (`criteria_version` is separate and unchanged).
Version bump: `triage-v3` -> `triage-v4` (stamped on `enquiries.prompt_version` / `llm_calls.prompt_version`).

## Feedback round 2 - follow-up email drafting (`draft-followup-v1`)
Capability 3 gained a follow-up variant (`prompts/draft_followup_system.md`, thread block in `draft_user.md.j2`); the first
email prompt stays `draft-v2`. See `capabilities/followup-email-drafts.md`.
