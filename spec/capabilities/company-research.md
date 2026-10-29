# Capability: Company Research
**Phase 3**. For CRM prospects this is now versioned, editable and run from the prospect page: see `prospect-research.md` (q15); compose without a prospect keeps the behaviour below. Composition in `agent.md` → Capability 2.

## What It Does
On request, an agent looks up a prospect company's website (and its LinkedIn page, if connected) and writes a short brief: what the company does, size hint, AI maturity hint, and three talking points.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Company name, website/LinkedIn hints | fields | Company record | Name |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Brief + sources | text | Company page, timeline, drafting context |
| Cost record (feature research) | record | Cost view |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Company website | HTTP GET (10s timeout, ≤200 KB) | Passed back to the agent as the observation; the brief may be partial |
| LinkedIn company page | via the team session, with the safety rules | Skipped if not connected or a stop occurs |
| Anthropic (Haiku) | ≤4 tool steps + finalize | Research row marked failed with the error |

## Business Rules
- At most 4 tool calls per research. The brief is ≤120 words and cites its source URLs.
- Research only runs when a user clicks it (no automatic research until Phase 4).

## Success Criteria
- [ ] Against the fixture company site, the brief mentions the fixture's stated industry and lists that site URL in its sources.
- [ ] An unreachable website still produces a brief, flagged "limited information".
- [ ] Each research run writes ≤5 `llm_calls` rows with feature `research`.
