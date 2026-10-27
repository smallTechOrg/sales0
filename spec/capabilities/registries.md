# Capability: Registries (Competitors + Channel Groups)
**Phase 2**

## What It Does
Maintains the Competitors list (website, name, approx #clients, client names, contact, email, notes) and the Groups list (name, platform WhatsApp, link, details keep/keep-low). These replace the corresponding sheet tabs.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Competitor fields | form | User / import | Name |
| Group fields | form | User / import | Name, platform |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Competitor and group lists with CRUD | pages | Competitors & Groups page; signals; Phase 5 WhatsApp |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none | — |

## Business Rules
- Five competitors are seeded: Future Labs, Savya Technologies, White Spaces, Evolvv, Ritu Arora.
- Group details are `keep`, `keep_low` or free text. Only active groups are ingested in Phase 5.

## Success Criteria
- [ ] After migration, 5 competitors are listed. Adding, editing and deleting one is reflected on reload.
- [ ] Creating a WhatsApp group with details `keep_low` shows that label.
