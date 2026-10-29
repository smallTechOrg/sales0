# Capability: Proactive Suggestions
**Phase 4**

## What It Does
Suggests new search keywords and WhatsApp groups to watch, and lists old prospects worth re-engaging (no activity for 60+ days, previously genuine).

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Recently accepted enquiries, active keywords, keyword yield | records | DB | Yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| ≤5 keyword suggestions with Add/Ignore | list | Setup + digest |
| Re-engage list | list | Pipeline |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Anthropic (Haiku) | inside the daily digest call | No suggestions that day |

## Business Rules
- Suggestions are never auto-added. Ignored suggestions are not repeated for 30 days.
- The re-engage list is deterministic (no LLM).

## Success Criteria
- [ ] Suggested keywords never duplicate an existing keyword (case-insensitive).
- [ ] A prospect with its last event 61 days ago appears in Re-engage. One at 59 days does not.
