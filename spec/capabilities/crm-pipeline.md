# Capability: CRM Pipeline (Prospects → Leads → Clients)
**Phase 2**

## What It Does
Moves accepted enquiries and shortlisted people through the binding lifecycle: Prospect (New → Contacted → Follow-up 1 → Follow-up 2 → Final follow-up), then Lead (Meeting scheduled → Proposal sent → Won / Lost / No response), then Client. Legacy clients can also be recorded.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Accepted enquiry | record | Review inbox | Yes (creates the prospect) |
| Stage change | action | Pipeline board/list | Yes |
| "Response received" flag | action | User | Required to convert to Lead |
| Legacy client | name, company, notes | User / import | No |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Prospects, leads, clients with their current stage | records | Pipeline page, Phase 4 metrics |
| Timeline events for every change | records | Timeline |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none | — |

## Business Rules
- Accepting an enquiry creates (or reuses) the company and a Prospect at New. The migration backfills prospects from enquiries accepted in Phase 1.
- A prospect becomes a Lead only after it has been contacted AND a response is recorded. Otherwise conversion is refused with that reason.
- A Lead with outcome Won becomes a Client (active). Lost / No response close it.
- Moving back a stage is allowed, and it is recorded.
- Each prospect has at most one open lead.

## Success Criteria
- [ ] Accepting an enquiry creates exactly one prospect at stage `new`, linked to the enquiry and the company.
- [ ] Converting a prospect at `new` to a lead without a recorded response is refused.
- [ ] contacted + response → lead `meeting_scheduled` → `proposal_sent` → `won` creates an active client, and each step writes a timeline event.
- [ ] Running the 0003 migration on a DB with 3 accepted enquiries creates 3 prospects.
