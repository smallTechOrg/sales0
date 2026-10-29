# Capability: Contacts
**Phase 2** · data: `data.md` → contacts, contact_emails, contact_phones, tags, contact_links

## What It Does
Maintains shared person records (several emails and phones, LinkedIn) that can be tagged and categorised, and links each person to enquiries, prospects, leads, clients, companies and competitors. It scales to thousands of people and lets the team merge duplicates.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Name, email(s), phone(s), LinkedIn URL, title, company | fields | User, accept hook, send hook, sheet sync, CSV import | A name, an email or a LinkedIn URL |
| Tags (name + category role/segment/custom) | list | User, CSV import | No |
| Link to an entity | entity ref + role | Automatic (accept, send, sheet) or user | No |
| Merge request (keep, drop) | action | Duplicates view | No |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Contact list (search, tag filter, company filter, 50/page) | page | Contacts page |
| Contact detail: emails, phones, tags, links, timeline | page | Contacts page |
| Duplicate groups | page | Contacts → Duplicates |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none | — |

## Business Rules
- **Matching on create** (`contacts.upsert`): any known email (case-insensitive, via `contact_emails`), then the LinkedIn slug (unique), then phone digits (last 10) together with a loosely matching name. Otherwise a new contact. A match fills empty columns and adds new emails/phones; it never blanks a value.
- The accept hook upserts the enquiry's contact person / author and links it (`role = author|buyer`); the send hook upserts each recipient (`role = recipient`); sheet rows upsert their name, emails, phones and LinkedIn (`role = sheet`).
- **Duplicates** are groups sharing an email, a LinkedIn slug, a phone, or an identical casefolded name + company. **Merge** moves emails, phones, tags and links, repoints `prospects.contact_id`, `enquiries.contact_id` and timeline rows to the kept contact, fills its empty columns, deletes the other and writes `contact_merged`. Merging a contact into itself is refused.
- Lists are paginated (`SALES0_CRM_PAGE_SIZE`, 50) and searchable by name, email, company or tag; search is one indexed-column `LIKE` over `search_text` plus one count and answers in under 300 ms at 10 000 contacts.
- Tags are unique by lower-cased name; a tag with no contacts is kept (counts shown).
- A contact fed by the sheet shows "Synced from sheet" next to sheet-owned names; its tags, notes and links are CRM-owned.

## Success Criteria
- [ ] Accepting two enquiries with the same email links both to a single contact; a second email is added to `contact_emails`, not a second contact.
- [ ] Filtering by tag "HR" returns only the tagged contacts; search by company name finds them.
- [ ] Merging B into A moves all of B's emails, tags, links and timeline to A and deletes B; self-merge → `merge_self` 400.
- [ ] Search over 10 000 seeded contacts returns in under 300 ms (integration timing assertion).
- [ ] The duplicates view lists two contacts that share a phone's last 10 digits only when their names loosely match.

- CRM feedback (`crm-entity.md`): e-mails / phones / LinkedIn are editable (also inline on the prospect page); the Companies list + roll-up page, company matching by domain / LinkedIn page and company merge.
