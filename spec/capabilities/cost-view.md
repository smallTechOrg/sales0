# Capability: Cost View
**Phase 4**

## What It Does
Shows AI spend by day and by month, broken down by feature (triage / research / drafting / suggest), with cost per enquiry and cost per accepted enquiry.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| LLM call records | records | `llm_calls` | Yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Daily and monthly totals, per-feature breakdown, averages | tables | Metrics → Cost |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none | — |

## Business Rules
- Totals use stored micro-USD, so a price change affects only new calls.
- Average cost per accepted enquiry = triage cost of all enquiries ÷ accepted count, for the period.

## Success Criteria
- [ ] With seeded calls of known cost, the daily/feature totals equal the sums exactly.
