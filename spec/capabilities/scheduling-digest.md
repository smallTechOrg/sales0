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

## Phase 2 additions (CRM sections, registered through `digest.register_section`; `services/crm/digest_sections.py`, S5)
- **Follow-ups due** (order 25): today's and overdue reminders, overdue first, each linking to the prospect (cap 10; stats `due_today`, `overdue`).
- **Stale leads** (order 26): open leads whose newest timeline event is older than `SALES0_STALE_LEAD_DAYS` (14) (stats `stale`, `open_leads`).
- **Pipeline movement** (order 27): new prospects, contacted, replies, leads opened, won inside the 24 h window.
- Each is built in a savepoint like the built-in sections; a CRM error shows an error item and the rest of the digest is intact. The sheet-sync job on the Schedule page is ready once the CRM repository is installed (still opt-in).

## Success Criteria
- [ ] With a frozen clock at a schedule time, a run with trigger `scheduled` is created. In needs-attention, none is created and a skip is logged.
- [ ] The digest for a seeded day lists the exact counts and the follow-ups due: with 2 reminders due today and 1 overdue, "Follow-ups due" has 3 items, overdue first, `stats == {"due_today": 2, "overdue": 1}`.
- [ ] A lead quiet for 19 days appears under "Stale leads" ("quiet for 19 days"); one quiet for 5 days does not.
