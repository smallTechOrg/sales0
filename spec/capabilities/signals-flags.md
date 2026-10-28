# Capability: Flags (Repeat Poster, Competitor Mention, No Follow-up)
**Phase 2** · data: `data.md` → flags, competitor_mentions (replaces the earlier "signals" design)

## What It Does
Flags prospects and enquiries that post requirements repeatedly ("keeps posting requirement"), that mention a known competitor, or that must not be followed up (an unsubscribe or a human decision). Flags are shown as badges and never decide anything on their own, except `no_follow_up`, which stops reminders.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Enquiries linked to a prospect / profile | records | Accept hook, discovery, sheet sync, import | Yes |
| Competitor registry (active competitors: name, website) | records | Registries | Yes |
| Unsubscribe reply | event | Reply tracking | For `no_follow_up` |
| User set / clear | action | Prospect page | No |

## Outputs
| Output | Type | Destination |
|---|---|---|
| "Repeat poster ×N" badge | flag | Prospect, Pipeline cards, contact/company pages |
| "Mentions <competitor>" badge (+ `competitor_mentions` link rows) | flag | Enquiry card, prospect page, competitor page |
| "Do not follow up" badge | flag | Prospect, Due-today panel (the prospect is excluded) |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none (deterministic) | — |

## Business Rules
- **Repeat poster:** the number of enquiries linked to the same prospect (or sharing its `linkedin_profile`) with `date_added` in the last `SALES0_REPEAT_POSTER_DAYS` (60) is ≥ `SALES0_REPEAT_POSTER_MIN` (3) → active flag with detail `{count, within_days}`. Falling below the threshold clears an `auto` flag. Recomputed when an enquiry is linked, after a sheet sync / import that touched enquiries, and once for old data at first startup.
- **Competitor mention:** an active competitor's name or website host appears (casefold, word boundary) in an enquiry's raw text, summary or company → a `competitor_mentions(enquiry)` row, the flag on the enquiry and on its prospect. A client can be linked to a competitor by hand. Deactivated competitors are ignored.
- **No follow-up:** set automatically by a human `unsubscribe` reply (`source = auto`, detail has the reply id) or by the user (`source = user`). Setting it cancels the open reminders; clearing it does not recreate them. User flags are never auto-cleared.
- One active flag per `(entity, kind)`; cleared flags stay in history.

## Success Criteria
- [ ] Three fixture enquiries from one author within 60 days → `repeat_poster` with count 3 on the prospect; two → none; the third aged 61 days → none.
- [ ] An enquiry containing "future labs" gets `competitor_mention` for Future Labs and a `competitor_mentions` row; a deactivated competitor does not match.
- [ ] A human `unsubscribe` reply → `no_follow_up` on the prospect, its reminders cancelled, it is absent from Due today.
- [ ] A user-set `no_follow_up` survives a recompute.
