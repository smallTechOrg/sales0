# Domain model: company, person, enquiry (migration `q17_company_model_c5e9a2d7`)

The team found four record types (Enquiry, Prospect, Lead, Client) confusing. The owner decided this model; the UI, services and sheet sync follow it.

## The three records

* **Company** is the main record. Prospect, Lead and Client are not separate records: they are the **stage** of one company.
* **Person** is a record of their own (a contact). A person can belong to several companies or to none, has a **role per company** (decision maker, HR / L&D, influencer, other; free text allowed) and is who outreach is sent to.
* **Enquiry** is one specific requirement (an opportunity). A company can have several. The word "Enquiry" is kept everywhere. Inbox review: *to review* -> *accepted* / *discarded*. After accept the enquiry has its own **deal status**.

## Company lifecycle

| Stage | Status inside the stage |
|---|---|
| **Prospect** | new -> contacted -> follow_up_1 -> follow_up_2 -> final_follow_up |
| **Lead** | meeting_scheduled -> proposal_sent |
| **Client** | active |

Off-ramps (the company is *closed*; a person can reopen it; the stage it was in is remembered and restored): **not_interested**, **dormant** (no response after the follow-ups), **do_not_contact** (blocks ALL outreach, drafting, sending and follow-up drafting; reopening needs an admin), **lost_client** (a former client that churned).

Rules:

1. Accepting an enquiry for a company we never dealt with creates it as Prospect / new. For an existing company the enquiry is attached and the stage is never demoted. A company with no enquiry may exist (cold list, sheet row); a company that only appears as someone's employer has no stage until someone presses *Add to pipeline*.
2. **Prospect -> Lead is a person's decision** ("Mark as lead"; no contact / response preconditions). The app may *suggest* it (a reply classified interested / question / not_now, an enquiry marked Meeting scheduled / Proposal sent, a meeting or proposal date from the sheet) as a visible "Suggested: Lead" hint the person accepts or dismisses. It never moves the stage by itself, and never reopens a closed company.
3. **Client**: a person marks any enquiry **Won** -> the company becomes a client and stays one (later enquiries are upsell). A person can also mark a company client directly (*Mark client*).
4. **Outreach and follow-ups belong to the company**, are sent to a chosen person and may be tagged "about enquiry X". The cadence (3 follow-ups) is per company. Stage / status changes are in the timeline.

## Enquiry deal status (after accept)

Vocabulary of the Active Enquiries sheet: `open, contacted, follow_up_1, follow_up_2, final_follow_up, meeting_scheduled, proposal_sent, won, lost, no_response, on_hold`. *Hot / Warm* are a temperature / priority tag, not a status.

* `open / contacted / follow_up_N / final_follow_up` of an open enquiry are **driven**: they mirror the company's prospect sub-status. They can be set by hand only on the company's ORIGIN enquiry (its earliest accepted enquiry), which then moves the company's prospect sub-status to the same stage (rule 5 above).
* `meeting_scheduled, proposal_sent, won, lost, no_response, on_hold` are set by a person (or by the sheet). A hand status stays until the person sets *Open* again (back to following the cadence).
* Meeting scheduled / Proposal sent offer (suggest) moving the company to Lead; Won makes the company a client.

5. **Origin-enquiry coupling.** The ORIGIN enquiry of a company is its earliest accepted enquiry (the one whose acceptance brought the company in). Setting its deal status to `contacted / follow_up_1 / follow_up_2 / final_follow_up` (by hand on the company page or the Enquiries board; the sheet through the same rule) moves the company's prospect sub-status to the same stage and writes the prospect / company / enquiry timeline entries. Only while the company is stage prospect and not on an off-ramp (otherwise the change is refused). By hand the move is exact (a person may also go back); any other source only moves forward, never past what outreach already recorded, and a stage set in the app (`stage_source = 'app'`) is never overwritten by the sheet. Non-origin enquiries never drive the stage (cadence statuses are refused on them). `meeting_scheduled / proposal_sent` still only show *Suggested: Lead* and a person confirms; Won makes the company a client. The company's own cadence still mirrors onto its driven / open enquiries (projection, Core SQL, never calls back), so there is no loop.

## Pipeline and pages

* **Pipeline** (`/pipeline?tab=enquiries|prospects|leads|clients`, default **Enquiries**; each tab label carries its count, each tab has a one-line "What is this?"). A company is in exactly one tab by its stage.
  * **Enquiries**: accepted enquiries grouped by deal status (open, contacted, follow_up_1, follow_up_2, final_follow_up, meeting_scheduled, proposal_sent, won), company name as the card label, a collapsed lane for lost / no_response / on_hold. Enquiries still to review stay in the Inbox. Each card has a status control; the ORIGIN enquiry also offers contacted / follow-up N (see the coupling rule below).
  * **Prospects** (stage prospect): columns New -> Contacted -> Follow-up 1 -> Follow-up 2 -> Final follow-up, a **Became lead** lane (companies that became leads in the last 30 days; cards link to the Leads tab), off-ramp prospects in a collapsed *Closed* lane. Never shows leads or clients in the other columns. Board | List layout, filters (search, priority, category, characteristic, flag, owner via the company page), "New company".
  * **Leads** (stage lead): Meeting scheduled -> Proposal sent, a **Won** lane (became clients in the last 30 days; links to Clients), closed leads in a collapsed lane.
  * **Clients**: a list (company, priority, characteristics, enquiries, owner, next follow-up); *Show closed* adds lost clients.
  * Old links: `?view=companies` = Prospects, `?view=enquiries` = Enquiries, `?stage=prospect|lead|client|closed` and `?tab=` pick the tab. The word *Companies* still names the company list page (`/companies`) and the record; only the pipeline tab is *Prospects*.
* **Company page** (`/companies/{id}`), one page for every stage: header (name, stage + status control, Mark as lead / Mark client / Close menu / Reopen, priority, characteristics, links, owner), People (role, e-mail / phone editor, add person), Enquiries (status control, source link, Won / Lost), Outreach & follow-ups, Research + account summary + notes, Timeline.
* Old URLs redirect: `/leads`, `/clients`, `/prospects` -> `/pipeline?tab=leads|clients|prospects`; `/prospects/{id}`, `/leads/{id}`, `/clients/{id}` and the `/pipeline/...` versions -> the company page.

## Storage (how it is built)

`companies` gained `stage, sub_status, off_ramp, off_ramp_reason, off_ramp_at, resume_stage, resume_sub_status, stage_source, stage_changed_at, lead_suggestion, owner_member_id`; `enquiries` gained `deal_status, deal_status_source`. The `prospects`, `leads` and `clients` tables stay as the company's **outreach + deal ledger** (follow-ups, outreach messages, research, notes, flags, characteristics and the timeline hang off them) and `services/crm/lifecycle.py` is the only service the UI uses to move a company; `db/company_projection.py` (a Session `after_flush` hook) derives the company columns from the ledger rows, so every writer (services, event hooks, sheet sync, importers) keeps them current. See `data.md` -> *Glossary and the company model*.

## Sheet sync (one-way, read-only; unchanged contract)

Prospects / Outreach / Working tabs -> companies at stage Prospect (sub-status from `status_map`); Active Enquiries -> enquiries (deal status per `status_map`, attached to a company); Contacts -> people; Competitors unchanged. The sheet owns a field until a person changes it in the app; an app value stands until the sheet cell itself changes. A stage set by a person (`stage_source = 'app'`) is not overwritten. An enquiry row saying Meeting scheduled / Proposal sent sets the enquiry status and the Lead suggestion; it does not create a lead.
