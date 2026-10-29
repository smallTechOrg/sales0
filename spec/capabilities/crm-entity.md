# CRM entity: characteristics, company roll-up, editable details, tracking people (team feedback, migration `q12_traits_9d2c4a71`)

Terminology (binding, from the user): a **Prospect is THE entity** (a company or a person). A **Client is the same
entity at a later stage**, not a different thing. "Training provider", "Partner", "Outreach client"... are
**characteristics** of that entity; one entity can have several, and partners / training providers can also be clients.

## B1 Characteristics
- **Registry** `characteristics` (name, `name_key` unique, `system_key` for the seeded rows, `active`, `sort_order`).
  Seeded: Training provider (`training_provider`), Partner (`partner`), Outreach client (`outreach_client`). Managed on
  `/competitors` (card `#characteristics-registry`, admin): add, rename, turn off / on. Turning one off hides it from the
  pickers; entities that have it keep it (shown as "retired" in their editor) and it can no longer be newly added.
- **Assignments** `entity_characteristics (characteristic_id, entity_type, entity_id)`, unique per triple.
  **Anchor rule:** stored on the prospect; a lead and a client that came from a prospect read and edit the prospect's set
  (so nothing is copied and nothing is lost on Prospect -> Lead -> Client). Only a client without a prospect
  (legacy / typed in) stores its own (`entity_type = 'client'`). Every change writes a `note` timeline event
  "Characteristics: + Partner; - Training provider" on the anchor.
- **Inbox Accept** (`templates/inbox/_card.html` Accept form): optional checkboxes for every active characteristic and
  **Already a client**. The AI hint pre-ticks Training provider when `ai_poster_type = training_agency` or
  `ai_opportunity_type = agency_requirement` (marked "(AI suggestion)"); the human decides. After the decision
  `prospects.apply_accept_choices` adds the ticked ones to the enquiry's prospect; "Already a client" calls
  `leads.promote_to_client`, which goes through the ONE lead -> client path: open (or reuse) the prospect's lead with
  `convert(require_contact=False)` and mark it Won (`move_stage(won)` -> `clients.create_from_lead`). Idempotent.
  Bulk accept carries no extras.
- **Edit later** on the prospect, lead and client headers (`#characteristics`, `POST /pipeline/{prospects|leads|clients}/{id}/characteristics`,
  `characteristic_ids` = the full new set). **Filter**: `characteristic=<id>` on `/pipeline` (board and list, prospects
  and leads), the Clients tab and `/companies`. Chips (`.trait`) show on board cards, list rows, clients and companies.

## B2 Company roll-up
- **Companies list** `/companies` (nav after Contacts): search by name or website, characteristic filter, per company the
  counts of prospects, clients, contacts, enquiries and open follow-ups (of its prospects and their leads) and the union of
  its entities' characteristics. JSON with `Accept: application/json`.
- **Company page** `/companies/{id}` is the roll-up: prospects (stage, status, flags, characteristics, open lead), leads,
  clients, every person (contacts of the company + contacts linked to its prospects, with e-mail / phone / LinkedIn), every
  enquiry (of the company or its prospects; the summary links to the prospect page `#enquiry-<id>`, else the Inbox, plus the
  post / author profile links), outreach messages and replies, open follow-ups, the timeline. Editable: name, website,
  LinkedIn company page, city, notes. Prospect, lead, client and contact pages link their company.
- **Matching** (`companies.find_match`, used by `get_or_create_row` and the sheet's `company_for`): same `name_key`, else
  same `domain_key` (bare website host, `_keys.domain_key`; generic hosts such as linkedin.com, gmail.com, bit.ly never
  match), else same `linkedin_key` (`company/<slug>` of a LinkedIn company page). Both keys are indexed columns on
  `companies`, backfilled by the migration with frozen helper copies (`tests/unit/crm/test_company_keys.py` keeps them equal).
- **Merge** (`POST /companies/{id}/merge`, admin, `drop_id`): moves prospects, leads, clients, contacts, enquiries, outreach
  messages, competitors, timeline rows, company contact-links and flags to the kept company, fills its empty website /
  LinkedIn / city, appends notes, deletes the duplicate and logs it on the timeline. Candidates offered: same domain, same
  LinkedIn page, or a name starting with the same word.

## B3 Edit details
- Contacts: add / change / remove / make-primary for e-mails (`/contacts/{id}/emails[/{eid}[/delete|/primary]]`) and phones
  (`/contacts/{id}/phones[...]`), plus name, title, company, LinkedIn profile (`in/` only, unique) and notes
  (`POST /contacts/{id}`; a field that is not posted is left alone). Normalised with the existing helpers
  (`_keys.first_email`, `_keys.display_phone` / `phone_digits`, `linkedin_slug_of`). `contacts.email` / `.phone` stay the
  primary values. An address owned by another contact answers `contact_conflict` (409: merge instead). A suppressed
  address shows a "Suppressed" badge (sending stays blocked by `suppressions`). Timeline events name the change, never the
  value. The same editor (`contacts/_editor.html`) is inline in the prospect page's Contacts card (`next` brings you back;
  only same-site `/contacts/`, `/pipeline/`, `/companies/` paths), which also has "Add a person".
- Prospect "Edit details": name, company, notes, company website, company LinkedIn page (edit the company row) and the
  person's LinkedIn profile (the prospect's identity; refused when another prospect owns it).
- **Sheet conflict rule** (extends audit #7 "a sheet value is written only when its cell changed"): on a sheet-fed
  enquiry's contact, only e-mails / phones the cell **newly lists** (not in the previous overlay) are added, and the contact
  LinkedIn link only when that cell changed; so an address removed or changed in the app is not re-added. A person row's
  **company is assigned only when the prospect is new, has none, or the Company cell changed** (it used to be reassigned on
  every sync). The prospect page's "Synced from sheet" panel says exactly this.

## B4 Adding tracked people
- `/tracked` has **Add person to track** (`#track-add-form`, `POST /tracked/add`: `profile_url`, optional `name`, `note`,
  `next`). `tracked_profiles.parse_profile_url` accepts only person profiles (`linkedin.com/in/...`, via
  `sales0.linkedin_url`) and explains what else was pasted (company / school page, search, post / feed, jobs, groups,
  `lnkd.in`, old `/pub/` link, non-LinkedIn). The `linkedin_profiles` row is created when missing (`ensure_profile`, no
  LinkedIn request, `posts_seen` untouched); `watch_note` keeps the note; then `track()`. A watch run fills the details.
- Track / Stop tracking on the contact page (`#contact-track`), the prospect page (existing toggle, with a hint when no
  profile is linked), a help line and a clearer empty state on `/tracked`.

## Success criteria (tests)
- [x] Characteristics set on a prospect show on its lead and its won client; only one row exists (on the prospect).
- [x] Accept with "Already a client" -> prospect converted, lead Won, one client (source `lead`), idempotent.
- [x] Filters by characteristic on prospects (board + list), leads, clients and companies.
- [x] Domain / LinkedIn-page matching; generic hosts never match; merge moves everything.
- [x] E-mail / phone add / change / remove / primary, conflicts, suppression badge; partial details post keeps notes.
- [x] A removed e-mail / phone is not re-added by a re-sync; a new address in a changed cell is; an app-set company stands
      until the Company cell changes.
- [x] Add person to track: valid URL tracked (row created, no network), company / search / non-LinkedIn URLs refused with
      the reason, repeat says "already tracked".
- [x] Migration `q12_traits_9d2c4a71`: seeds, backfill, no drift, re-runnable, downgrade.
