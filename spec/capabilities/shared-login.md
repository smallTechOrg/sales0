# Capability: Shared Login
**Phase 2**

## What It Does
Protects the whole app behind one shared team password, so it can run on a VPS. Records are already designed for per-user accounts (Phase 5).

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Password | string | Login form | Yes when `SALES0_APP_PASSWORD` is set |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Signed session cookie (14 days) | cookie | Browser |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none | — |

## Business Rules
- When a password is set, every page and action except `/login`, `/api/health` and static files requires the session. Unauthenticated requests redirect to `/login` (pages) or get 401 (JSON/htmx).
- When no password is set, the app binds to loopback only (the Phase 1 behaviour).
- 5 failed attempts per minute per IP → 60s lockout.
- Comparison is constant-time. The password is never logged.

## Success Criteria
- [ ] With a password set, GET `/inbox` → 303 `/login`. The correct password → inbox. A wrong password → the error message and no cookie.
- [ ] The 6th wrong attempt within a minute returns the lockout message.
- [ ] `/api/health` stays reachable without login.
