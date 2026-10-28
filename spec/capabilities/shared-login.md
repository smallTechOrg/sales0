# Capability: Shared Login (superseded by [per-user-accounts](per-user-accounts.md) in Phase 5)
> Phase 5 replaced the shared password by per-user accounts. What remains valid from this file: the open paths, the unauthenticated behaviour by caller, the CSRF notes and the cookie flags. The password/session/throttle rules below are historical: `SALES0_APP_PASSWORD` is now only the bootstrap password of the first admin.
**Phase 2** · code: `web/auth.py`, `web/routes/auth.py` (S3)

## What It Does
Protects the whole app behind one shared team password so it can run on a VPS. The "Acting as" member selector stays: it says WHO is acting, the login says whether you may use the app at all. Per-user accounts are Phase 5.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| `SALES0_APP_PASSWORD` | env (SecretStr) | `.env` | No: unset (or empty) = the app is open, loopback only |
| Password | form field | `/login` | Yes when a password is set |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Session cookie `sales0_session` (HttpOnly, SameSite=Lax, Secure on https, `SALES0_SESSION_DAYS` = 14) | cookie | Browser |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none | — |

## Business Rules
- **Open paths** (no session needed): `GET/POST /login`, `POST /logout`, `/api/health`, `/static/*`, and `/__fixtures/*` only when `SALES0_LINKEDIN_MODE=fixture` (the fixture LinkedIn is served by the app to its own headless browser). Everything else requires a valid session.
- **Unauthenticated behaviour** by caller: a normal page request → `303 /login?next=<path+query>`; an htmx request → `401` with `HX-Redirect: /login` (htmx performs a full navigation) and an empty body; a JSON caller (`/api/*` or `Accept: application/json`) → `401` `{"ok": false, "error": {"code": "unauthorized", "message": "Please log in."}}`; a plain form POST → `303 /login`. `next` is honoured only when it is a same-origin path (starts with a single `/`, no scheme, no `//`).
- **Password unset:** the middleware is a pass-through; `/login` redirects to `/`; no Log out control is shown. Startup still refuses a non-loopback `SALES0_HOST` without a password (`check_startup`).
- **Session value** = `crypto.sign("v1:<issued_at>:<password fingerprint>", purpose="session")` using `SALES0_SECRET_KEY` (so cookies survive restarts when the key is set; with the generated dev key they survive as long as `data/secret.key` does). Valid when the signature verifies, the age is below `session_days`, and the fingerprint equals the current password's (changing the password logs everyone out).
- **Comparison** is constant-time (`hmac.compare_digest` on SHA-256 digests). The password is never logged (`http_request` logs form FIELD NAMES only), never echoed, never in a URL.
- **Throttle:** `SALES0_LOGIN_MAX_ATTEMPTS` (5) failures within 60 s from one client IP (`request.client.host`; `X-Forwarded-For` is not trusted) → `429` with "Too many attempts - try again in N seconds" for `SALES0_LOGIN_LOCKOUT_S` (60), even for a correct password during the lockout. A success resets the counter. The throttle is in memory (a restart clears it).
- **CSRF stays as built:** the Origin/Referer same-origin check on every unsafe method, now also accepting the origin of `SALES0_PUBLIC_BASE_URL`. The session cookie is SameSite=Lax. `/login` and `/logout` pass the same check. No per-form token is added (Assumed).
- **Logout** (`POST /logout`) clears the cookie and `303 /login`. The acting-as cookie is untouched.

## Success Criteria
- [ ] With a password set: `GET /inbox` → 303 to `/login?next=%2Finbox`; the correct password → 303 to `/inbox` with a session cookie; a wrong password → 401 page with "Wrong password" and no cookie.
- [ ] The 6th wrong attempt within a minute → 429 lockout message, even with the right password; after the lockout (clock advanced) the right password works.
- [ ] `GET /api/health` → 200 without a session; an htmx request without a session → 401 + `HX-Redirect: /login`; `GET /api/runs/1` without a session → 401 JSON envelope.
- [ ] Changing the configured password invalidates an existing cookie; a tampered or expired cookie is rejected.
- [ ] `next=https://evil.example` and `next=//evil.example` redirect to `/inbox`, not off-site.
- [ ] Without a password every page behaves as in Phase 1 and `/login` → 303 `/`.
- [ ] Cross-origin POST `/login` is rejected by the CSRF check (403 `csrf_rejected`).
