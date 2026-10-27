# sales0 — AI-Native Design

**Conclusion: AI augments the product** (outcome 2 of the lens). sales0 is a conventional CRUD + scraping app. Its core value comes from specific AI capabilities:

- Phase 1: enquiry triage, with a learning loop.
- Phase 3: company research and outreach drafting.
- Phase 4: digest and suggestions.

Each capability is a separately composed, bounded agent behind the `LLMClient` layer. Every one uses the configured Haiku model only (`SALES0_LLM_MODEL`, default `claude-haiku-5-5`), and every call is logged with tokens and cost. Provider access is the Anthropic API key (`SALES0_ANTHROPIC_API_KEY`) plus, for org-level keys that are not workspace-scoped (this user's key), `SALES0_ANTHROPIC_WORKSPACE_ID`, sent as the `anthropic-workspace-id` header. A missing workspace id surfaces as a fatal `LLMAuthError` naming the variable (`architecture.md` → LLM layer contract).

## Lens evaluation (catalogue in `agentic-ai.md`)

| # | Pattern | Decision | Rationale |
|---|---|---|---|
| 1 | Prompt chaining | **Used, in a degenerate form (one structured call + repair edge)** | Triage is a fixed transform (post → verdict + extraction) with all context in-prompt and no tools to call. The catalogue allows going below the ReAct floor for exactly this case. One batched call keeps cost minimal (user constraint). |
| 2 | Routing | Rejected | All enquiries share one prompt. Poster-type routing would add calls without improving accuracy. |
| 3 | Parallelization | Rejected (Phase 1) | Volume is ~50–150 posts a day. Sequential triage inside the run thread is fast enough and avoids rate-limit bursts. Revisit only if runs become slow. |
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
| 16 | Resource-aware optimisation | **Used** | Haiku only. One call per enquiry. Inputs truncated (`SALES0_TRIAGE_MAX_INPUT_CHARS`), few-shot capped (`SALES0_FEWSHOT_MAX_EXAMPLES`, `…_TEXT_CHARS`). Duplicates are never re-triaged. Output capped at 900 tokens (`SALES0_LLM_MAX_OUTPUT_TOKENS`). |
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

1. **build_prompt** renders `prompts/triage_system.md`. The system prompt contains the role, smallTech's business, the decision rules, the priority rules, the output-field rules, 3 static seed examples (genuine buyer, agency recruiting freelance trainers, individual-trainer-seeker), and the active offerings list as `id | name | group | description`. It then renders `prompts/triage_user.md.j2`: the few-shot block, then the enquiry (author, headline, date, url, text truncated to `SALES0_TRIAGE_MAX_INPUT_CHARS`). Every untrusted string placed inside the prompt's delimiters (few-shot example text and reasons, and all enquiry fields) is delimiter-escaped first: `<` → `&lt;`, `>` → `&gt;`, so post text cannot open or close a delimiter. It builds a `StructuredRequest(feature="triage", tool_name="record_triage", input_schema=TriageResult JSON schema, max_tokens=SALES0_LLM_MAX_OUTPUT_TOKENS, prompt_version, log_context={enquiry_id, fewshot_count, input_summary})`.
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

**Concurrency:** sequential, one triage at a time inside the single job thread. `retry_triage` from the UI runs synchronously in the request (one call, ~2–5 s, with a spinner). It is refused with `job_busy` while a run is triaging that same enquiry.

**Finalize / persistence (`services/triage.py`, not the agent):** it writes the `ai_*` fields, initialises the editable fields (`priority`, `offering_id`, `company`, `contact_person`, `email`, `phone`, `city`, `mode`, `requirement_summary`) from the result, and sets `triage_status`, `triaged_at`, `prompt_version`. It inserts one `llm_calls` row per response (feature `triage`, `enquiry_id`, `run_id`, tokens, `cost_usd_micros`, `latency_ms`, `fewshot_count`, `status`) and one `status_history` row (`triage: pending → done|failed`).

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
- The cost bound: ≤6 × 600 chars ≈ ≤1k extra input tokens per call (≈$0.001 at the default prices).

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

## Capability 3 — Outreach drafting (Phase 3)

**Composition:** a single structured call (#1) over a per-offering template, using the company brief and timeline as context (#8), with HITL approval (#13) and guardrails (#18).

The tool is `record_draft(subject, body_markdown, deck_id | null)`. Guards: it must not invent prices, it must not exceed 180 words, and it must contain no unfilled `{{placeholders}}`. The output is a draft only. Sending requires an explicit human Approve on that exact version. Any edit after approval requires approval again.

## Capability 4 — Digest + suggestions (Phase 4)

**Composition:** a single structured call (#1, #21). Deterministic SQL computes yesterday's numbers and today's follow-ups. Haiku writes a 5-bullet digest and proposes ≤5 new keywords, derived from the language of recently accepted enquiries that is absent from the active keywords. The suggestions are shown with Add/Ignore buttons. Nothing is auto-added.

## Phase 5

WhatsApp ingestion reuses Capability 1 unchanged: `source="whatsapp_group"`, with the same dedupe and the same triage. Reply tracking is deterministic (Gmail thread polling), with no LLM.
