# Capability: Reply Tracking
**Phase 5**

## What It Does
Detects replies to emails sent from sales0, records them on the timeline, and marks the prospect as responded (which allows Lead conversion).

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Sent email thread ids | records | Email outreach | Yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Reply events + "Response received" | records | Timeline, pipeline |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Gmail API | poll threads every 15 min | Logged; retried next cycle |

## Business Rules
- Only messages in a known thread, from a non-team sender, count as replies. Auto-replies (out of office) are flagged and do not count.

## Success Criteria
- [ ] A thread with a reply from the prospect address creates a `reply` event and sets responded.
- [ ] An "Automatic reply" subject does not set responded.
