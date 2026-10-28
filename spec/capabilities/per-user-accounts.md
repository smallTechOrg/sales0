# Capability: Per-User Accounts
**Phase 5** · code: `services/team/{accounts,passwords,members,acting}.py`, `web/auth.py`, `web/acting.py`, `web/routes/{auth,users}.py` · migration `p7_accounts_a1d4c6e8`
(supersedes [shared-login](shared-login.md) - the open paths, unauthenticated behaviour and CSRF rules of that file still hold)

## What It Does
Every person logs in with their own email and password. **Team members ARE the users**: one table (`team_members`) holds the person, their role, their Gmail connection (Phase 3) and now their password. The logged-in member is the actor on every decision, stage change, timeline event and send ("Accepted by Priya"). Admins manage people on the **Users** page. WhatsApp ingestion is deferred (user decision), not part of this build.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Email + password (+ "Remember me") | form | `/login` | Yes (unless open mode) |
| Name, email, role `admin`/`member` | form | Users page (admin) | Yes |
| One-time set-password link `/set-password/<token>` | URL | admin hands it over (nothing is emailed) | for first-time setup and resets |
| `SALES0_APP_PASSWORD` | env | `.env` | No - BOOTSTRAP only (below) |
| `SALES0_TRUSTED_PROXIES` | env, comma-separated IPs/CIDRs | `.env` | No |
| `SALES0_SESSION_DAYS` (14), `SALES0_SESSION_REMEMBER_DAYS` (30), `SALES0_PASSWORD_RESET_TTL_H` (24), `SALES0_PASSWORD_MIN_LENGTH` (10) | env | `.env` | No |

## Outputs
| Output | Type | Destination |
|---|---|---|
| `actor_user_id` (= `team_members.id`) / `credited_by` / timeline `actor` + `actor_member_id` on every new record | data | DB, Pipeline and Timeline views show the member's name |
| Cookie `sales0_session` (HttpOnly, SameSite=Lax, Secure on https) | cookie | Browser |

## Business Rules
- **Modes.** *Open mode* = no member has a password and `SALES0_APP_PASSWORD` is unset: no login, the header selector is free, a banner says "Open mode - anyone who can reach this app can use it. Add a password on the Users page." *Accounts mode* = at least one member has a password (or the bootstrap variable is set): every path except the open paths needs a live session. `check_startup` still refuses a non-loopback host in open mode.
- **Bootstrap.** If `SALES0_APP_PASSWORD` is set and no ADMIN has a password yet, it is hashed as the password of the admin `SALES0_ADMIN_EMAIL` / `SALES0_ADMIN_NAME` (member created if missing) at startup, or at the first login attempt if the app was not started through its lifespan. After any admin has a password the variable is ignored for login. Changing it later does nothing.
- **Passwords** are argon2id hashes (`argon2-cffi` defaults), minimum `SALES0_PASSWORD_MIN_LENGTH`. A login for an unknown email still burns one hash verification (no user enumeration by timing). Every login failure says "Wrong email or password.".
- **Sessions are server-side.** `member_sessions` stores only the SHA-256 of a random id; the cookie is that id signed with `SALES0_SECRET_KEY`. Lifetime `session_days`, or `session_remember_days` with "Remember me". The id **rotates** when older than 15 minutes (the previous id keeps working for 60 s so parallel htmx requests do not log out); expiry slides on rotation. Logout deletes the row. **Deactivating a member, forcing a reset, a reset via link and changing one's own password delete the member's sessions** (a self change keeps the current one).
- **Set-password links.** An admin creates one on the Users page (when adding a member, or "Set-password link / Force reset"). The raw token is shown ONCE on the response; only its SHA-256 is stored (`password_tokens`); it is single use (claimed atomically), expires after `password_reset_ttl_h`, and a newer link invalidates older ones. Issuing a link for a member who already has a password sets `must_reset` (old password refused, sessions revoked) until they choose a new one. Using the link never logs in: the member is sent to `/login`.
- **Throttle.** Failed logins are limited per (client IP, email) at `SALES0_LOGIN_MAX_ATTEMPTS` per 60 s -> 429 for `SALES0_LOGIN_LOCKOUT_S`, plus a looser per-IP bucket (4x) against spraying many emails. Set-password link probes use the same guard under one key.
- **Client IP.** The TCP peer, unless the peer is in `SALES0_TRUSTED_PROXIES`; then the RIGHT-MOST `X-Forwarded-For` entry that is not itself trusted (entries to its left are client-supplied). A malformed entry falls back to the peer. uvicorn's `proxy_headers` stays off; the parsing is ours.
- **Roles.** `admin` can open `/users` and manage members; `member` gets 403. Admin rights are the REAL logged-in user's, also while acting as someone else. The last active admin cannot be deactivated or demoted; nobody can deactivate themselves.
- **Acting as.** The header shows the logged-in member (name + role, link to My account) and Log out. An **admin** may pick another member from "Act as" (signed cookie `sales0_acting`) so that work is credited to that member; for everyone else the cookie is ignored and `POST /team/acting` returns 403. When an admin acts as Y the record carries both: `actor_user_id`/`actor_member_id` = Y (the credit), timeline `actor` = "X as Y" with `data.by_member_id` = X, and status-history rows get the note "by X (acting as)". In open mode the selector is free, as in Phase 3.
- **Attribution audit.** Every mutating web route takes the acting member (or is listed with a reason in `tests/integration/members/test_actor_audit.py`, which enumerates all routes). Historical records with a null actor show "team".
- **Unification migration (p7).** The Phase 1 `users` table is retired; the five `actor_user_id` FKs (enquiries, keywords, scrape_runs, status_history, triage_decisions) now point at `team_members.id` (`ON DELETE SET NULL`). Legacy `users` rows become inactive team members and attribution is remapped; dangling ids become NULL.

## Success Criteria
- [ ] Admin adds a member, copies the one-time link, the member sets a password and logs in; the link is gone after one use.
- [ ] A member's accept writes `actor_user_id`, and the Pipeline timeline shows their name.
- [ ] A member cannot open `/users` (403) and cannot act as someone else.
- [ ] Deactivating a member kills their session at their next request.
- [ ] The migration keeps every row of a copy of the live database and leaves no FK violation.
