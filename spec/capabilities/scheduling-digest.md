# Capability: Scheduled Runs + Daily Digest
**Phase 4**

## What It Does
Runs discovery automatically at up to 3 configured times a day, within every Phase 1 safety cap. It produces a daily digest: yesterday's numbers, follow-ups due today, and the AI-written highlights.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Schedule times (≤3/day) + enabled | settings | Runs page | No |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Scheduled runs (trigger `scheduled`) | records | Runs page |
| Digest | text | Home/inbox panel |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| LinkedIn | as in discovery | Same safety stops. While the session needs attention, the scheduler pauses itself. |
| Anthropic (Haiku) | one digest call per day | The digest shows the numbers without highlights |

## Business Rules
- A random ±15 min jitter is applied to each scheduled time.
- A scheduled run is skipped (and logged) if a cap would be exceeded or a job is busy.
- The scheduler runs in-process (single worker) and catches up at most one missed slot after a restart.

## Success Criteria
- [ ] With a frozen clock at a schedule time, a run with trigger `scheduled` is created. In needs-attention, none is created and a skip is logged.
- [ ] The digest for a seeded day lists the exact counts and the follow-ups due.
