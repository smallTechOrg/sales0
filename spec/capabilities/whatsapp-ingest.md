# Capability: WhatsApp Group Ingestion
**Phase 5**

## What It Does
A linked-device (WhatsApp Web) bot reads messages from the 10–30 selected groups and feeds requirement-like messages into the same dedupe → triage → inbox flow.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| QR link of the team WhatsApp | one-time scan | User | Yes |
| Group selection | list from Registries (active groups) | User | Yes |
| Messages | text + sender + group + time | Bridge | Yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Enquiries with source `whatsapp_group` | records | Inbox |
| Bridge status (linked, last message) | status | Header / WhatsApp page |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| WhatsApp Web (via the bridge) | read-only | The bridge reports unlinked; the status turns red and a banner shows. sales0 never posts to groups. |

## Business Rules
- Read-only: the bot never sends messages.
- Messages under 80 characters, or with no requirement keywords from the active keywords list, are dropped before triage (a cost guard).
- Dedupe by message id and content hash (so a message cross-posted to many groups is one enquiry).

## Success Criteria
- [ ] Posting a fixture payload of 3 messages (one cross-posted to 2 groups, one too short) to the ingest endpoint creates 1 enquiry with 2 group hits, and drops 1.
