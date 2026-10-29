# Capability: Profile Outreach Discovery
**Phase 3**

## What It Does
Lets the team shortlist people for outreach from LinkedIn profiles, found from posts or from a pasted profile URL. It replaces the Outreach / Prospects working sheet (Name, LinkedIn URL, Company, Website, Status, Reason of adding, Notes).

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Profile URL or "shortlist author" on an enquiry | action | User | Yes |
| Reason of adding (post link) | url | Automatic / user | No |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Profile candidate (name, headline, company, website) | record | Outreach page; becomes a Prospect (kind outreach_person) |
| Status: New / Connection request sent / Message sent / Closed | field | Outreach page |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| LinkedIn (team session) | open one profile page, read name/headline/company | Same safety stops, caps and pacing as discovery. Profile views count against a separate daily cap (default 30). |

## Business Rules
- sales0 never sends connection requests or messages on LinkedIn. The status is updated by the user.
- A profile URL is unique. Adding it again shows the existing record.

## Success Criteria
- [ ] Fixture profile `/in/fixture-priya` is scraped into name "Priya Menon" and company "Acme Logistics".
- [ ] Hitting the profile cap refuses further scrapes with the cap message.
- [ ] A checkpoint during a profile scrape sets the session to needs-attention.
