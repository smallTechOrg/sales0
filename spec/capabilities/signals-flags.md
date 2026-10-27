# Capability: Signals (Repeat-Poster + Competitor Mentions)
**Phase 2**

## What It Does
Flags authors and companies that post requirements repeatedly ("spams many requirements" / "keeps posting requirement"), and flags enquiries that mention a known competitor.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Enquiries (author profile URL, company, text) | records | Discovery, import | Yes |
| Competitor registry | records | Registries | Yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| "Repeat poster ×N" badge | signal | Inbox card, contact page |
| "Mentions <competitor>" badge | signal | Inbox card, company page |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none (deterministic) | — |

## Business Rules
- Repeat poster: the same author profile URL (or company name key) appears on ≥3 enquiries within 60 days.
- Competitor mention: a competitor name or website domain appears in the raw text (case-insensitive, word boundary).
- Signals are computed after each run and after each import. They are shown, and never decide anything on their own.

## Success Criteria
- [ ] 3 fixture enquiries from the same author within 60 days give that author a `repeat_poster` signal with count 3. Two enquiries give none.
- [ ] An enquiry containing "Future Labs" gets a `competitor_mention` signal for that competitor.
