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
- (Phase 1.1) A keyword whose search was running when LinkedIn stopped a run (logout, checkpoint, captcha or rate limit) shows that stop time as its "Last run" time. The next run therefore searches it last, not first.
- (Phase 1.1) The keywords `checkpoint-test`, `captcha-test`, `ratelimit-test`, `ratelimit-scroll-test`, `checkpoint-scroll-test`, `lazy-scroll-test`, `layout-drift-test` and `logout-test` only have a special meaning in fixture mode. In live mode they are ordinary keywords, and no keyword text is rejected for that reason.
- (Phase 1.1) The read-only **Safety limits** on Setup also list the browser in use and both safety cool-downs (`architecture.md` → Configuration).

## Success Criteria
- [ ] After migration, `/setup` lists exactly: AI corporate workshops, Technical AI training (Training), AI consulting & build services (Services), Products (Products).
- [ ] Adding "  Corporate   AI training " stores "Corporate AI training". Adding "corporate ai training" afterwards returns the `keyword_duplicate` toast, and the list still has one row.
- [ ] Adding a 1-character keyword shows the `keyword_invalid` toast.
- [ ] Removing a keyword after a run leaves the run's per-keyword row showing the keyword text.
- [ ] Creating the offering "AI audits" in the new group "Services+" makes it show under that group, and the next triage prompt contains "AI audits".
- [ ] Deleting an offering used by an enquiry returns "deactivated". The offering vanishes from the selects, and that enquiry card still shows its name.
- [ ] Deleting an unused offering removes its row.
- [ ] (Phase 1.1) After a fixture run whose first keyword is `checkpoint-test`, `/setup` shows that keyword's "Last run" as the stop time. The next run's plan lists it last.
- [ ] (Phase 1.1) With the default cool-down settings (the test sets 60/15 explicitly), `/setup` Safety limits contain "Cool-down after rate limit / captcha" with "60 min", "Cool-down after checkpoint / logout" with "15 min", and a "Browser" row. They no longer contain "Browser window during runs".
