# Capability: Contacts
**Phase 2**

## What It Does
Maintains shared person records (email, phone, LinkedIn) that can be categorised and tagged, and links each person to enquiries, prospects, leads, clients and competitors. It must scale to thousands of people.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Name, email, phone, LinkedIn URL, title, company | fields | User, triage extraction, import | Name or email or LinkedIn URL |
| Tags / category | list | User | No |
| Link to an entity | entity ref + role | User / automatic on accept | No |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Contact list (search, filter by tag, paginated) | page | Contacts page |
| Contact detail with links + timeline | page | Contacts page |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none | — |

## Business Rules
- On accept, a contact is created or matched from the extracted contact person, email and phone. Matching uses email (case-insensitive), then LinkedIn URL, then phone digits.
- Duplicate contacts can be merged. Their links and timeline are combined.
- Lists are paginated (50 per page) and searchable by name, email, company or tag. Search must respond in under 300 ms at 10k contacts.

## Success Criteria
- [ ] Accepting two enquiries with the same email links both to a single contact.
- [ ] Filtering by the tag "HR" returns only the tagged contacts.
- [ ] Merging contact B into A moves all of B's links to A and deletes B.
- [ ] Search over 10,000 seeded contacts returns in under 300 ms (integration timing assertion).
