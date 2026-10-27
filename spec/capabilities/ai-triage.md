# Capability: AI Triage (Verdict, Extraction, Offering Match, Learning Loop, Cost)
**Phase 1**. Composition in `agent.md` → Capability 1.

## What It Does
For each new enquiry, one AI call returns everything the inbox card needs:

- a genuine/discard verdict with a one-line reason
- the poster type
- the requirement summary
- the priority
- the best-matching offering
- the extracted contact and requirement details

Each call learns from the team's past accept, discard and override decisions, and records its token cost.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Enquiry text + author, headline, date, link | text | Discovery | Yes |
| Active offerings catalogue | list | Search Setup | Yes (may be empty → offering null) |
| Past team decisions | list (latest per enquiry) | Review inbox | No (cold start uses built-in examples) |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Verdict genuine/discard + one-line reason (≤160 chars) | fields | Inbox card |
| Poster type: buyer company / training agency / individual-trainer-seeker / job seeker or self-promo / other | field | Inbox card |
| Requirement summary "Company - Topic - City/Mode" | field | Inbox card |
| Priority high/medium/low | field | Inbox card (editable) |
| Offering match (or none) | field | Inbox card (editable) |
| Company, contact person, email, phone, city, mode, dates, topic, language | fields | Inbox card |
| Token counts + USD cost per call | record | Card cost, run cost, header totals |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Anthropic API (configured Haiku model only) | one structured call per enquiry; at most one repair call if the answer is malformed | Transient errors are retried with backoff. Still failing → enquiry "triage failed", with Retry. Invalid key or model → the run stops triaging with a message pointing at `.env`. |

## Business Rules
- Exactly one AI call per new enquiry (plus at most one repair). Duplicates are never re-triaged.
- **Discard** covers: agencies or marketplaces recruiting freelance trainers (the dominant historical case), people seeking an individual trainer at hourly or per-day rates, trainers advertising themselves, requests far outside the offerings ("too specific"), and non-requirements. **Genuine** means a company or team wanting to buy training, workshops, consulting/build or a product.
- Contact details are only ever copied from the post. An email or phone that does not literally appear in the post text is dropped (anti-hallucination). An offering id outside the active catalogue is dropped.
- A discard verdict always gets low priority.
- Only the one-line reason is shown. The AI's other text is not displayed.
- **Learning loop**: each call includes up to N recent team decisions as examples (default 6). Overrides (cases where the team disagreed with the AI) are included first. The rest alternate between accepts and discards, and each example is truncated (default 600 chars). Every call records how many examples it used.
- The AI's own answer is stored unchanged (the `ai_*` fields) alongside the team-editable values, so overrides stay measurable.
- Cost per call = input tokens × input price + output tokens × output price. Prices come from configuration. Per-enquiry cost is the sum of its calls. Today and month totals use the configured timezone.
- Every call is logged with feature, model, tokens, cost, latency, status and example count.

## Success Criteria
- [ ] Real Haiku on the fixture corpus (integration) gives these results:
  - P1: genuine, buyer_company, email `training@acmelogistics.example`, mode offline, city contains "Pune", priority high.
  - P2, P3, P9, P11: discard.
  - P6: genuine with email and phone null.
  - P8: offering = AI consulting & build services.
  - P10: offering = Technical AI training.
  - P5 (Hindi + emoji): genuine, with a non-empty summary.
- [ ] Every verdict's `reason` is a single line of 1–160 chars. Every `requirement_summary` is 1–140 chars.
- [ ] A guard unit test: a model answer with email `ceo@invented.example` that is absent from the text is stored as null.
- [ ] A guard unit test: offering_id 999 → null. A discard with priority high → stored low.
- [ ] Repair unit test (FakeLLMClient): an invalid first answer followed by a valid second one gives status done with 2 llm responses. Two invalid answers give status failed, "AI returned an invalid answer twice".
- [ ] `select_examples` unit test: from 3 overrides + 5 accepts + 5 discards with max 6, it returns 3 overrides plus an alternating discard/accept/discard fill, in chronological order, each ≤600 chars.
- [ ] After decisions exist, every triage `llm_calls` row of the next run has `fewshot_count ≥ 1`. On a fresh DB it is 0.
- [ ] Each triaged enquiry has ≥1 `llm_calls` row with input_tokens > 0, output_tokens > 0, and cost_usd_micros equal to the formula on those tokens.
- [ ] An invalid API key (integration, with `SALES0_ANTHROPIC_API_KEY=invalid` in that test's env only) makes a run end `failed` with "Anthropic rejected the key". The saved enquiries remain `pending`.
