# Capability: Search Setup (Keywords + Offerings Catalogue)
**Phase 1**

## What It Does
Lets the team manage the LinkedIn search keywords, and the grouped catalogue of smallTech offerings that triage matches against.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Keyword text | string 2–100 chars | Setup form | Yes (add) |
| Keyword active flag | bool | Setup toggle | No |
| Offering name | string 1–80 | Setup form | Yes |
| Offering group name | string 1–60 (existing or new) | Setup form | Yes |
| Offering description | string 0–500 | Setup form | No |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Keyword list | rows (text, active, last run) | Setup page; consumed by discovery runs |
| Offerings list grouped by group | rows | Setup page; triage prompt; inbox offering selects |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none | — |

## Business Rules
- Keyword text is trimmed and inner whitespace is collapsed to single spaces. Uniqueness is case-insensitive: "AI Workshop" and "ai workshop" are the same keyword.
- Removing a keyword keeps its history. Run logs and enquiry keyword chips keep the text snapshot.
- Inactive keywords are skipped by runs.
- Four offerings are seeded on first migration, in three groups (see `data.md`). All four can be edited and deleted.
- An offering referenced by any enquiry cannot be hard-deleted. It is deactivated instead and the user is told why. Inactive offerings disappear from triage and from the inbox selects. Existing enquiries keep showing the name.
- Typing a new group name creates the group. Empty groups are hidden.
- Offering names are unique (case-insensitive).
- Triage always uses the active catalogue at the moment of the call.

## Success Criteria
- [ ] After migration, `/setup` lists exactly: AI corporate workshops, Technical AI training (Training), AI consulting & build services (Services), Products (Products).
- [ ] Adding "  Corporate   AI training " stores "Corporate AI training". Adding "corporate ai training" afterwards returns the `keyword_duplicate` toast, and the list still has one row.
- [ ] Adding a 1-character keyword shows the `keyword_invalid` toast.
- [ ] Removing a keyword after a run leaves the run's per-keyword row showing the keyword text.
- [ ] Creating the offering "AI audits" in the new group "Services+" makes it show under that group, and the next triage prompt contains "AI audits".
- [ ] Deleting an offering used by an enquiry returns "deactivated". The offering vanishes from the selects, and that enquiry card still shows its name.
- [ ] Deleting an unused offering removes its row.
