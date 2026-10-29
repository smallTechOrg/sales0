# Data-integrity + sheet-sync audit (read-only, @7ae3d66). Sheet sync read-only guard VERIFIED; apply twice = 0 changes; CRM-owned survive.
1 HIGH sheetsync/parsers.py:319 + mapping.py:405-435 (_find_enquiry/_hash_for): editing summary/email of a row WITHOUT post link/Full Enquiry (eh: key) creates NEW enquiry+prospect, soft-removes old, old prospect stays open w/ human history. Fix: adopt by (company+contact+date_added) or previous row_number before creating; soft-remove/close prospect when its last enquiry removed and untouched by humans.
2 HIGH services/crm/prospects.py:380-405 ensure_for_enquiry + services/inbox.py:300-317: two simultaneous accepts of profile-less enquiry -> 2 prospects. Fix: unique index prospects.origin_enquiry_id (partial where not null) and/or BEGIN IMMEDIATE + re-read.
3 MED inbox.decide not idempotent: re-accept inserts 2nd TriageDecision + status_history (pollutes few-shot learning). Fix: no-op if already in target status.
4 MED sheetsync/values.py:125-162 _nearest_past: year-less dates shift +1 year when a row is edited later (and 29 Feb). Fix: persist inferred year in sheet_fields, reuse when day/month unchanged.
5 MED mapping.py:756 + values.py:222/parsers.py:366: sheet company URL becomes in/<slug> person; key li:<slug> drops kind. Fix: use crm._keys.linkedin_slug_of(); keep kind in key.
6 MED services/reply_classify.py:110-147 lacks ensure_within_spend_cap; calls labelled feature 'triage' (inflates cost_per_enquiry costs_report.py:211). Fix: cap check -> skipped_deferred; distinct llm_calls.feature value (widen CHECK via migration) e.g. 'reply'.
7 MED mapping.py:466-489 Writer._apply: any sheet edit overwrites in-app edits of sheet-owned enquiry columns silently. Fix: write only fields whose sheet value changed since last overlay; record old->new in sheet_updated event data.
8 MED README lines 279 & 555 claim `alembic downgrade -1` also works but 0004_opportunity_type.py:37-39 and 0003_phase1_4_seen_posts.py:434-454 downgrades cascade-delete child rows (batch rebuild with FK ON). Fix: wrap with PRAGMA foreign_keys=OFF / rebuild / foreign_key_check / ON (p6 recipe) or document restore-from-backup-only.
9 L-M engine.py:144 allow_mass_removal never exposed. Fix: `sync-sheets --allow-mass-removal` + 'Apply anyway' button on /sync (admin).
10 L-M parsers.py:252-261 duplicate natural keys re-keyed by order -> flip-flop when reordering/deleting. Fix: enquiry is identity; first row wins or field merge.
11 L seen_posts purge not a scheduled job (discovery_job.py:582, watch_job.py:261 only); NULL raw_purge_after never purged. Fix: register scheduler purge hook calling purge_expired; backfill NULL.
12 L seen_posts.py:677-686 purge strips text of pending/failed rows leaving them pending forever. Fix: mark failed('raw_purged') or exclude.
13 L values.py:222/265 vs scraper/urls.py profile_slug: no percent-decoding/NFC/host check/scheme-less. Fix: one canonicaliser used everywhere (unquote, NFC, casefold, host check, /in /company /pub).
14 L inbox.decide/update_fields write status_history not timeline_events; 2nd accept/discard-without-prospect/priority edits leave no timeline event. Fix: emit timeline event for every decision.
15 L crm/hooks.py:26-36 _guarded swallows failed hook -> accepted enquiry w/o prospect silently. Fix: audit event + nightly/startup healer for accepted enquiries with prospect_id NULL.
16 L seen_posts.py:249 uses UTC date for relative post dates; mapping.py:743 Client.started_on UTC; engine.py:59 default UTC. Fix: settings.tz.
17 L README line 278 mentions -wal/-shm contradicting Backups section (line 511).
18 L no in-flight accounting for spend cap (bounded overshoot) - document.
19 INFO LIKE '%..%' scans (mapping.py:405, dedupe._near_duplicate_of) O(N): store+index activity id later.
