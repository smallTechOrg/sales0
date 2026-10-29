# Capabilities Index

| Capability | Phase | File | One line |
|---|---|---|---|
| Search Setup | 1 | [search-setup.md](search-setup.md) | Keywords CRUD + grouped offerings catalogue (4 seeded) |
| LinkedIn Discovery | 1 | [linkedin-discovery.md](linkedin-discovery.md) | Connect once, Run now with safe pacing, dedupe, run log, stop on any account-risk signal |
| AI Triage | 1 | [ai-triage.md](ai-triage.md) | One Haiku call per enquiry: verdict + reason + extraction + priority + offering, few-shot from decisions, cost tracked |
| Review Inbox | 1 | [review-inbox.md](review-inbox.md) | Accept → Prospect · New / Discard (kept, searchable) / Override, bulk, search |
| CRM Pipeline | 2 | [crm-pipeline.md](crm-pipeline.md) | One prospect per LinkedIn profile → Lead (after contact + response) → Client; driven by accept / send / reply events |
| Contacts | 2 | [contacts.md](contacts.md) | Shared, taggable people (many emails/phones) linked to every entity; merge duplicates; thousands |
| Timeline + Follow-ups | 2 | [timeline-followups.md](timeline-followups.md) | Full history per record; reminders +3/+7/+14 days; Due today / Overdue |
| Flags | 2 | [signals-flags.md](signals-flags.md) | Repeat-poster, competitor-mention, no-follow-up |
| Registries | 2 | [registries.md](registries.md) | Competitors + groups (sheet-fed, editable), competitor links |
| Sheet Sync (read-only) | 2 | [sheet-sync.md](sheet-sync.md) | One-way Google Sheet → CRM sync; sheet wins its fields; weekly/secondary baseline; Sync page |
| Manual Paste + CSV Import | 2 | [sheet-import.md](sheet-import.md) | Paste one post; import app-owned CSVs (legacy clients, contacts) |
| Shared Login | 2 | [shared-login.md](shared-login.md) | One team password, signed session cookie, VPS-ready |
| Profile Outreach Discovery | 3 | [profile-outreach-discovery.md](profile-outreach-discovery.md) | Shortlist people from LinkedIn profiles |
| Company Research | 3 | [company-research.md](company-research.md) | ReAct agent writes a company brief |
| Email Outreach | 3 | [email-outreach.md](email-outreach.md) | Template + deck + AI draft + human approval + Gmail send (built; Phase 2 adds "Draft email" from a prospect and the send hook) |
| Metrics Dashboard | 4 | [metrics-dashboard.md](metrics-dashboard.md) | Weekly + Secondary sheets automated, funnel, yield (merges the Phase 2 `sheet_metrics` baseline) |
| Cost View | 4 | [cost-view.md](cost-view.md) | AI spend per day/month/feature |
| Scheduling + Digest | 4 | [scheduling-digest.md](scheduling-digest.md) | Scheduled runs within caps; daily digest (Phase 2 adds follow-ups-due, stale-leads, pipeline-movement sections) |
| Proactive Suggestions | 4 | [proactive-suggestions.md](proactive-suggestions.md) | Keyword/group suggestions; re-engage old prospects |
| WhatsApp Ingestion | 5 | [whatsapp-ingest.md](whatsapp-ingest.md) | Linked-device bot feeding the same triage |
| Per-User Accounts | 5 | [per-user-accounts.md](per-user-accounts.md) | Individual logins and attribution |
| Reply Tracking | 5 | [reply-tracking.md](reply-tracking.md) | Detect email replies, mark responded (built; the Phase 2 CRM subscribes to its event) |
| Hosted Deployment | deploy | [hosted-deployment.md](hosted-deployment.md) | Cloud Run + Cloud SQL Postgres, no scheduler (manual request-bound actions), laptop `sales0 worker` scrapes and uploads |
| CRM entity (team feedback) | 2+ | [crm-entity.md](crm-entity.md) | Characteristics on the entity (survive Prospect -> Lead -> Client), accept straight to client, Companies list + roll-up + merge, editable e-mails / phones, Add person to track |
