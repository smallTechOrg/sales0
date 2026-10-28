# Capability: Registries (Competitors + Channel Groups)
**Phase 2** · data: `data.md` → competitors, competitor_mentions, channel_groups

## What It Does
Maintains the Competitors list (website, name, approx #clients, client names, contact, email, notes) and the Groups list (name, platform, link, details keep/keep-low). Both are fed by the Google Sheet (Competitors and Groups tabs, read-only) and can also be edited in the app; competitors link to the enquiries and clients that mention them.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Competitor fields | form | User, sheet sync, CSV import | Name |
| Group fields | form | User, sheet sync, CSV import | Name |
| Manual competitor ↔ enquiry/client link | action | User | No |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Competitor and group lists with add / edit / delete | page | Competitors & Groups page |
| Competitor detail with linked enquiries and clients | page | Competitors & Groups page |
| Active competitors / groups | records | Flags (mention detection); Phase 5 ingest (active groups) |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none | — |

## Business Rules
- Five competitors are seeded by the migration: Future Labs, Savya Technologies, White Spaces, Evolvv, Ritu Arora. A sheet row adopts a seeded competitor by website, else by name (no duplicates).
- **Sheet-fed rows:** the sheet owns name, website, approx clients, client names, contact, email and the sheet notes (edits there overwrite the app); the app owns `notes`, `active`, links and flags. Delete on a sheet-fed row is refused (`sheet_owned`); deactivate it, or remove it in the sheet (it is then soft-marked removed and hidden under "Removed from sheet").
- A competitor needs a non-blank name. Deleting one removes its mention links, not the enquiries.
- Group `details` is the raw text; `keep` and `keep-low` are normalised into `keep_level` (`keep` / `keep_low`); `platform` is lower-cased (default `whatsapp`). Only active groups are ingested in Phase 5.
- Competitor mention detection uses active competitors only (`signals-flags.md`).

## Success Criteria
- [ ] After migration, 5 competitors are listed; adding, editing and deleting an app-created one is reflected on reload.
- [ ] After a sheet sync of a fixture "Competitors" tab that contains "Future Labs", there are still 5 + (new rows) competitors, "Future Labs" has a sheet source, and deleting it returns `sheet_owned`.
- [ ] A group with details `keep-low` shows the "Keep (low)" label and `keep_level = keep_low`.
- [ ] A client linked to a competitor appears on the competitor's page.
