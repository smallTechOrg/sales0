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

* `open / contacted / follow_up_N / final_follow_up` of an open enquiry are **driven**: they mirror the company's prospect sub-status. Nobody sets them by hand.
* `meeting_scheduled, proposal_sent, won, lost, no_response, on_hold` are set by a person (or by the sheet). A hand status stays until the person sets *Open* again (back to following the cadence).
* Meeting scheduled / Proposal sent offer (suggest) moving the company to Lead; Won makes the company a client.

## Pipeline and pages

* **Pipeline** (`/pipeline`): default = companies as cards in Prospect | Lead | Client (sub-status chip, priority, characteristics, owner, next follow-up date), off-ramp companies in a collapsed *Closed* lane. A toggle switches to the **Enquiries** board by deal status (company name on each card). Filters (priority, characteristic, category, stage, flag) and the list view stay.
* **Company page** (`/companies/{id}`), one page for every stage: header (name, stage + status control, Mark as lead / Mark client / Close menu / Reopen, priority, characteristics, links, owner), People (role, e-mail / phone editor, add person), Enquiries (status control, source link, Won / Lost), Outreach & follow-ups, Research + account summary + notes, Timeline.
* Old URLs redirect: `/leads`, `/clients`, `/prospects` -> `/pipeline?stage=...`; `/prospects/{id}`, `/leads/{id}`, `/clients/{id}` and the `/pipeline/...` versions -> the company page.

## Storage (how it is built)

`companies` gained `stage, sub_status, off_ramp, off_ramp_reason, off_ramp_at, resume_stage, resume_sub_status, stage_source, stage_changed_at, lead_suggestion, owner_member_id`; `enquiries` gained `deal_status, deal_status_source`. The `prospects`, `leads` and `clients` tables stay as the company's **outreach + deal ledger** (follow-ups, outreach messages, research, notes, flags, characteristics and the timeline hang off them) and `services/crm/lifecycle.py` is the only service the UI uses to move a company; `db/company_projection.py` (a Session `after_flush` hook) derives the company columns from the ledger rows, so every writer (services, event hooks, sheet sync, importers) keeps them current. See `data.md` -> *Glossary and the company model*.

## Sheet sync (one-way, read-only; unchanged contract)

Prospects / Outreach / Working tabs -> companies at stage Prospect (sub-status from `status_map`); Active Enquiries -> enquiries (deal status per `status_map`, attached to a company); Contacts -> people; Competitors unchanged. The sheet owns a field until a person changes it in the app; an app value stands until the sheet cell itself changes. A stage set by a person (`stage_source = 'app'`) is not overwritten. An enquiry row saying Meeting scheduled / Proposal sent sets the enquiry status and the Lead suggestion; it does not create a lead.
