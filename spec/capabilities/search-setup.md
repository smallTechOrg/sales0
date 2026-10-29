# Capability: Search Setup (Keywords + Offerings Catalogue)
**Phase 1; Phase 1.4 adds the buyer-intent keyword library and per-keyword yield stats**

## What It Does
Lets the team manage the LinkedIn search keywords (seeded with a library of buyer-intent phrases), and the grouped catalogue of smallTech offerings that triage matches against.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Keyword text | string 2–100 chars | Setup form | Yes (add) |
| Keyword active flag | bool | Setup toggle | No |
| Keyword edit (text, group) | strings | Setup row (Phase 1.4) | No |
| Add suggested keywords click | action | Setup (Phase 1.4) | No |
| Offering name | string 1–80 | Setup form | Yes |
| Offering group name | string 1–60 (existing or new) | Setup form | Yes |
| Offering description | string 0–500 | Setup form | No |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Keyword list | rows (text, group, active, last run, scanned/relevant yield) | Setup page; consumed by discovery runs |
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

## Phase 1.4 — Buyer-intent keyword library and yield stats
Evidence: the single topic keyword "agentic ai training" found 9 posts, 5 new, and Haiku rightly discarded all 5 (event promos and thought leadership). A TOPIC finds people talking about AI; a BUYER-INTENT phrase finds people asking for it. The library therefore ships phrases in the language of real enquiries. Topic-only phrases stay allowed (the user can add any), but none is seeded.

- **Seed library.** 60 phrases, 15 per offering group (list below), stored as ordinary keywords with `group_name` and a stable `suggested_key` (slug of the phrase: lower-case, non-alphanumerics → `-`, ≤60 chars). Defined in code in `services/keyword_library.py` (`SUGGESTED_KEYWORDS`), not in the migration.
- **Idempotent seeding that never overwrites edits.** `seed_suggested(db)` inserts a suggested phrase only when no keyword row has its `suggested_key` AND no keyword has the same `text_key`. It never updates, re-activates, re-groups or re-texts an existing row. A user's edit of a seeded keyword's text keeps its `suggested_key`, so the original is not added again. A user's own keyword with the same text is left as is (no `suggested_key`). Seeding runs (a) once at first startup after 0003 (guarded by `app_meta.keywords_seeded_v1`, and off when `SALES0_SEED_SUGGESTED_KEYWORDS=false`) and (b) on the **Add suggested keywords** button, which also re-adds suggested phrases the user deleted (it is an explicit request) and reports "Added N · M already present".
- **Editable.** Each keyword row can be edited (text, group) in place, toggled active, or removed, as before. Edited text goes through the same normalisation and duplicate check (`keyword_duplicate`).
- **Grouped.** The Keywords card lists keywords under the 4 offering names (suggested) and "Your keywords" (no group), each group collapsible.
- **Yield stats per keyword**, updated at the end of each keyword's scan: `scanned_total`, `relevant_total` (posts the AI called genuine), `last_scanned`, `last_relevant`, `last_run_at`. A row shows "Scanned 412 · Relevant 6 (1.5%) · last run 2h ago". A keyword scanned ≥ 80 posts across runs with 0 relevant is badged "No yield" (a hint to turn it off; nothing is deactivated automatically).
- **Planning.** A run takes up to `SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN` (40) active keywords, un-run keywords of an interrupted run first, then least-recently-run (never-run first). With 60 seeded keywords every phrase is searched about every 1.5 runs.

**The 60 suggested phrases** (group → phrases):
- **AI corporate workshops**: looking for AI trainer · corporate AI training · urgent trainer requirement AI · GenAI workshop for employees · AI enablement workshop · AI training partner · AI upskilling program · AI corporate workshop · AI training for managers · AI workshop for leadership team · ChatGPT training for our team · corporate generative AI training · AI awareness session for employees · AI literacy workshop for teams · looking for AI training vendor
- **Technical AI training**: Claude Code trainer · agentic AI workshop · build AI agents training for developers · LLM training for engineers · prompt engineering workshop for company · Claude Code training for developers · AI agent workshop for engineering team · RAG workshop for developers · hands-on GenAI bootcamp for engineers · AI coding assistant training · LangChain training for team · LLM application development training · AI upskilling for software engineers · agentic AI training for engineers · GenAI hackathon workshop for tech team
- **AI consulting & build services**: hiring AI consultant · build AI agent for our company · AI automation partner · need chatbot developer company · looking for AI development company · RAG chatbot over company documents · AI automation agency for business · GenAI implementation partner · AI solution for customer support · AI consulting firm India · enterprise AI assistant development · AI proof of concept partner · AI strategy consultant for company · need AI agent development vendor · custom AI chatbot for our website
- **Products**: AI tool for our sales team · AI meeting notes tool for company · AI knowledge base software for company · AI document search tool for enterprise · AI chatbot platform for business · AI customer support software · AI workflow automation platform · AI copilot for our employees · GenAI platform vendor for enterprise · AI tool for HR team · AI lead generation tool · private LLM for our company · AI content generation tool for marketing team · AI analytics assistant for business · AI voice agent for our business

> **Assumed:** these phrases are the seed, chosen from the wording of real enquiries in the team's sheets. The team edits them freely; the per-keyword yield stats tell which to keep. Fixture mode (offline) matches them to the synthetic bulk corpus (`architecture.md` → Thorough discovery).

## Success Criteria
- [ ] (Phase 1.4) `seed_suggested` on an empty keyword table adds exactly 60 rows (15 per group, all active, all with `suggested_key`). A second call adds 0. After the user edits one seeded keyword's text, deletes another and adds their own "corporate AI training" (before seeding), `seed_suggested` leaves the edited row's text untouched, does not duplicate the edited one, and does not create a duplicate of the user's own; the button path re-adds the deleted one.
- [ ] (Phase 1.4) `/setup` shows the **Add suggested keywords** button (`#add-suggested-keywords`); pressing it on a fresh DB lists the 60 phrases under 4 group headings with a toast "Added 60 · 0 already present", and pressing it again toasts "Added 0 · 60 already present".
- [ ] (Phase 1.4) After a run, a keyword row shows its scanned/relevant counts and last-run time, equal to the run's per-keyword row; `scanned_total` accumulates across runs.
- [ ] After migration, `/setup` lists exactly: AI corporate workshops, Technical AI training (Training), AI consulting & build services (Services), Products (Products).
- [ ] Adding "  Corporate   AI training " stores "Corporate AI training". Adding "corporate ai training" afterwards returns the `keyword_duplicate` toast, and the list still has one row.
- [ ] Adding a 1-character keyword shows the `keyword_invalid` toast.
- [ ] Removing a keyword after a run leaves the run's per-keyword row showing the keyword text.
- [ ] Creating the offering "AI audits" in the new group "Services+" makes it show under that group, and the next triage prompt contains "AI audits".
- [ ] Deleting an offering used by an enquiry returns "deactivated". The offering vanishes from the selects, and that enquiry card still shows its name.
- [ ] Deleting an unused offering removes its row.
- [ ] (Phase 1.1) After a fixture run whose first keyword is `checkpoint-test`, `/setup` shows that keyword's "Last run" as the stop time. The next run's plan lists it last.
- [ ] (Phase 1.1) With the default cool-down settings (the test sets 60/15 explicitly), `/setup` Safety limits contain "Cool-down after rate limit / captcha" with "60 min", "Cool-down after checkpoint / logout" with "15 min", and a "Browser" row. They no longer contain "Browser window during runs".
