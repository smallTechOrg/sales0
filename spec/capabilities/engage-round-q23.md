# Capability round q23: company categories, Refresh all, company offerings, research people, drafting instructions, follow-up log + reminders

Migration `q23_engage_5c1d8e33`: `company_offerings`, `reminders`, `follow_up_notes`, `text_settings`, and the
"Direct client" characteristic (system key `direct_client`).

## Categories live on the company
- The enquiry category (relevant prospect / channel partner / competitor) is no longer shown or edited: removed from
  Inbox cards and filter, the Pipeline filter and chips, and the company page enquiry rows. (Triage still stores its
  suggestion; an AI-suggested competitor still gets the "File in Competitors" offer.)
- The company's category is a characteristic: **Partner** or **Direct client**. The account summary (`summary-v3`)
  returns `relationship` (partner / direct_client / unknown) and `fit_offerings`; `crm/auto_tags.apply` ticks the
  characteristic only when the company has neither, and tags offerings only when it has none (`source = ai`). Research
  ends with a summary refresh, so research tags too.
- **Refresh all N / Refresh selected** (Companies page, `POST /companies/research` with `mode=refresh`): the summary +
  tags from current info, no new web research; a summary a person edited is kept (only tagged). Chunked like research
  (4x the research chunk per click) with a Continue form.

## Offerings
- **Setup > Offerings** (`/setup/offerings`): the catalogue by group. Groups can be renamed (renaming to an existing
  name merges the two) and reordered; offerings are added / edited / switched off as before. Two or more offerings
  share a category by sharing a group.
- A company can fit **several offerings** (`company_offerings`): editor on the company page, an Offerings column and
  filter on Companies, an Offering filter on the Pipeline (company boards / lists by tag; the Enquiries board by the
  enquiry's offering or its company's tag), and chips on pipeline cards. Research all / Refresh all respect the filter.

## Research: top people
- Web search (`research-web-v3`) also looks for the founder / CEO and the heads of HR, L&D, talent, technology and the
  enquiry's department, with LinkedIn profile URLs and PUBLIC business contact details.
- Synthesis (`research-synth-v2`) returns `key_people` (up to 8). Code keeps a person only with a known source, and a
  LinkedIn URL / email / phone only when it appears verbatim in the input (no guessed email patterns). They are saved
  as the company's contacts (found-or-created, empty fields filled, linked to the prospect; origin `import`, actor
  "AI research") and shown on the Research card.
- Not done: opening each person's LinkedIn profile through the laptop worker (contact info there is only visible to
  connections and would add page loads on the team's account).

## Drafting instructions
- **Outreach > Instructions** (`/outreach/instructions`, admin to save): free text appended to the drafting system
  prompt for every AI draft (first emails and follow-ups); the fixed rules win on conflict.

## Follow-ups: a free-text log + reminders
- The follow-ups card starts with **Log a follow-up**: free text -> one small AI call (`followup-note-v1`) -> How
  (email / call / WhatsApp / LinkedIn / meeting / other), Date (never future), Contact (matched to the company's
  people), Notes, and a reminder when the text asks for a next date. The **log** lists every entry (structured, with
  the text as typed). If the AI is unavailable the note is kept as typed. A first note on a New prospect moves it to
  Contacted.
- Nothing is pre-scheduled: `SALES0_FOLLOWUP_AUTO_SCHEDULE` (default `false`) turns the old Follow-up 1 / 2 / Final
  scheduling off; existing scheduled follow-ups still show and work.
- **Reminders** (`services/reminders.py`, table `reminders`) are a reusable module for any record (`subject_type` +
  `subject_id`, optional company / prospect): add, done, change date, cancel, open / due lists. Due reminders appear in
  the Pipeline Due panel and the nav badge; the follow-ups card lists the prospect's open ones and can add one by hand.
