# Capability: Metrics Dashboard (Weekly + Secondary sheets)
**Phase 4**

## What It Does
Automatically produces the founder's Weekly metrics per ISO week, plus the Secondary per-source metrics against weekly targets, funnel conversion, and keyword/group yield.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Enquiries, decisions, prospects, leads, timeline, outreach | records | All modules | Yes |
| Weekly targets | numbers (editable) | Seeded: LinkedIn scraping 15, WhatsApp scraping 5, LinkedIn outreach 5, messages sent 25, first follow-ups 20 | Yes |
| Manual counts (blogs/posts published, people added to WA group, old prospects looked up) | numbers per week | User | No |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Weekly table per ISO week: enquiries added, enquiries contacted, meetings/proposals with pricing, old prospects looked up, blogs/posts published, people added to WA group | table | Metrics page, CSV export |
| Secondary: per-source enquiries added vs target, messages sent, first follow-ups vs target | table with progress bars | Metrics page |
| Funnel (enquiry → accepted → contacted → lead → won) and keyword/group yield (accepted ÷ seen) | charts/tables | Metrics page |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | SQL over the full data (no sampling, no LLM) | — |

## Business Rules
- Weeks are ISO weeks in the configured timezone.
- Each metric is computed from the full data, never from a sample.

## Success Criteria
- [ ] On a seeded fixture of ≥300 enquiries over 6 ISO weeks with known counts, every weekly cell equals the precomputed value.
- [ ] The keyword yield for a keyword with 10 seen / 3 accepted shows 30%.
