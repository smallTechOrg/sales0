# Security audit (read-only, @7ae3d66) — no critical/high. Fix list for the fixer
1 MED  web/middleware.py:23-31 `_input_summary` reads whole urlencoded body pre-auth (80MB -> RSS 15->331MB); /outreach/decks multipart parsed fully before 10MB cap. Fix: Content-Length cap (1MB; larger only for upload paths) before reading; cap/skip body read in _input_summary.
2 MED  outreach approve/send by admin 'acting as' X records only X (routes/outreach.py:475-540, services/outreach/messages.py approve/send/add_event). Spec per-user-accounts.md:33 requires 'X as Y'. Fix: pass acted_by_id into outreach events/approved_by, or block approve/send while acting.
3 MED  members can administer shared resources: require_admin only in users.py + 2 oauth routes; setup.py (LinkedIn accounts, li_at paste, owner_member_id never set at setup.py:374), runs, schedule, sync, outreach templates/decks, registries, importer unguarded. Fix: require_admin on LinkedIn accounts/cookie/schedule/sync/run-control/offerings/keywords-bulk routes (or ownership).
4 MED  open mode readable via DNS rebinding (GET /team with Host: evil.example = 200). Fix: in open mode reject non-loopback/non-configured Host on all methods.
5 MED  research fetcher DNS rebinding window (agent/research/fetch.py:128-145 check vs :222 connect). Fix: pin validated IP / re-check peer IP after connect.
6 L-M  set-password tokens in logs (web/middleware.py:70 logs path). Fix: redact /set-password/*.
7 L-M  data dir 755 / DB 644 (config.py, web/app.py:68, db/engine.py:39). Fix: chmod 700 dir, 600 db/-wal/-shm at startup.
8 L-M  suppressions exact-address only (services/suppressions.py). Fix: canonical form (strip +tag, gmail dots) + domain-level suppression.
9 L    secret key min length not enforced (crypto.py master_secret / config.py:79). Fix: validator >=16 chars or valid Fernet key.
10 L   /account/password unthrottled (routes/auth.py:182). Fix: LoginGuard.
11 L   sessions slide forever (services/team/accounts.py:224-254). Fix: absolute max (90d).
12 L   CSV export prefix misses \t \r (services/metrics/exports.py _safe).
13 L   no security headers/CSP. Fix: X-Frame-Options DENY, Referrer-Policy no-referrer, CSP default-src 'self' (htmx inline? verify), nosniff.
14 L   deck PDF served inline same-origin (routes/outreach.py:677-681). Fix: Content-Disposition attachment + CSP sandbox.
15 L   non-session cookies lack Secure; Secure conditional on public_base_url https (web/acting.py:79-98, config.py). Fix: derive from scheme/X-Forwarded-Proto w/ trusted proxies or SALES0_COOKIE_SECURE.
16 L   /api/health leaks db_revision/model/linkedin_mode unauthenticated. Fix: minimal when unauthenticated.
17 L   message-id enumeration (404 vs 403). Fix: 404 for non-owner non-admin.
Config risks: proxy without SALES0_TRUSTED_PROXIES shares one IP bucket; LINKEDIN_MODE=fixture on public host opens /__fixtures (refuse at startup); htmx 2.0.11 vendored (check advisories).
