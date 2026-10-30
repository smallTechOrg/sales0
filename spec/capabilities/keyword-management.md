# Capability: Keyword Management and AI Keyword Ideas
**Keywords page (`/keywords`).** Builds on `search-setup.md` (keyword CRUD on Setup still works) and
`proactive-suggestions.md` (the Haiku Suggestions page stays). Composition in `agent.md` -> keyword ideas.

## What It Does
One page for every LinkedIn search keyword: what it found, where those enquiries went, and quick edits.

- **Organise by** performance tier (default), offering, enquiry outcome, or one table; sort by tier, relevant, rate,
  accepted, won/leads, scanned, newest, A-Z; filter by text, tier, status, offering. Filters live in the URL.
- **Per keyword**: scanned, relevant, rate, and over the DISTINCT enquiries it found (`enquiry_keyword_hits`, matched by
  keyword id, else by text for older / imported hits): accepted, contacted (enquiry status past Open), leads (company is a
  lead or client) and won (status Won or company a client).
- **Tiers** (`services/keyword_insights.py`, decided in code): winner (2+ accepted, or 3+ relevant at 10%+), promising
  (1+ relevant), new (< 30 scanned), dead (80+ scanned, nothing relevant), underperforming (the rest).
- **Manual editing**: inline edit (text + offering), Active/Paused toggle, bulk add (one per line or comma separated,
  duplicates and invalid ones reported), bulk pause / activate / move to offering / remove (with confirmation).
- **AI keyword ideas** (`agent/keyword_ideas.py`, prompt `keyword-ideas-v1`): ONE call on `SALES0_KEYWORD_IDEAS_MODEL`
  (default `claude-opus-5-5`) over the active offerings, every keyword's numbers and tier (top 200 by accepted /
  relevant), up to 30 winning posts (accepted first, then genuine) and 15 noise posts (discarded / AI discard), each
  with the keyword that found it, plus an optional focus line. It returns new keywords (new / variant / replacement,
  offering, reason, evidence post numbers), existing keywords to retire (pause or reword, with a better phrase) and
  wording that attracts noise. **Code decides**: phrases are normalised, 2-100 characters, never a duplicate of ANY
  existing keyword or of each other; offerings must match by name; evidence numbers must exist; a keyword to retire
  must exist; at most 20 ideas and 10 retirements. Nothing is stored: the answer is a review list (ticked by default,
  text and offering editable); **Add ticked ideas** adds them and pauses the ticked retirements.

## Business Rules
- Reading is open to every member; every change needs an admin (as on Setup).
- Post text is third-party DATA inside `<post>` tags (inert-escaped); only the system prompt defines behaviour.
- The call is checked against the spend cap first; one `llm_calls` row (feature `suggest`), priced with
  `SALES0_KEYWORD_IDEAS_PRICE_*` (defaults 5 / 25 USD per 1M tokens - verify against current pricing).
- AI unavailable -> a clear error, nothing changes; a bad key / model -> "check the API key and SALES0_KEYWORD_IDEAS_MODEL".
