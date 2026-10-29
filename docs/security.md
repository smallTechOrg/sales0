# Security notes (web infrastructure)

Fixes for the security audit findings that touch the web layer (group A). Each has a regression test.

| # | Finding | Fix | Tests |
|---|---------|-----|-------|
| 1 | Unbounded request bodies read pre-auth | `HardeningMiddleware` (outermost, pure ASGI): 1 MB cap on every body; larger only for `POST /import/csv` (5 MB + 64 KB) and `POST /outreach/decks` (`SALES0_DECK_MAX_BYTES` + 64 KB). Checked from `Content-Length` before anything reads; chunked bodies go through a counting `receive` that stops at the cap. 413 via `error_response`. The request-log middleware no longer reads a body without a declared, in-limit length. | `test_security_infra.py::test_oversized_*`, `test_chunked_*`, `test_normal_sized_*`; unit `test_body_limits_*` |
| 4 | DNS rebinding in open mode | `AuthMiddleware` checks the `Host` header (port-agnostic) on every method. Open mode: loopback + `SALES0_HOST` only. Logins enabled: also the `SALES0_PUBLIC_BASE_URL` host and `SALES0_ALLOWED_HOSTS`. Other hosts: 400. `/static/*` is exempt (public assets). The Starlette TestClient host `testserver` is accepted only with `SALES0_ALLOW_TESTSERVER_HOST=true` (set in `tests/conftest.py` `TEST_ENV`; never on a server). | `test_open_mode_rejects_a_foreign_host_on_every_method` (GET /team with `Host: evil.example`), `test_login_mode_allowlist_*`, `test_testserver_host_is_refused_*` |
| 6 | Set-password tokens in logs | `redact_path()` masks `/set-password/<token>` in the `http_request`, `csrf_rejected`, `request_too_large`, `domain_error` and `unhandled_exception` log lines (uvicorn's access log is already disabled). | unit `test_set_password_token_is_redacted_in_logged_paths` |
| 7 | Data dir 755 / DB 644 | `harden_storage()` (called in the app lifespan): data dir 0700; SQLite db, `-wal`, `-shm` and `secret.key` 0600. Best effort, errors ignored. | unit `test_harden_storage_*` |
| 9 | Weak secret key | `SALES0_SECRET_KEY` must be >= 16 characters (a Fernet key is 44) or startup stops with a clear message; blank = unset. | unit `test_short_secret_key_*`, `test_secret_key_16_chars_*` |
| 10 | `/account/password` unthrottled | Uses the same `LoginGuard` as login (key: member email + `#password-change`); only a wrong *current* password counts as a failure. | `test_account_password_change_is_throttled_like_login` |
| 11 | Sessions slide forever | Absolute 90-day lifetime from `member_sessions.created_at` (no migration); sliding renewal is capped at it and the row is deleted when reached. | `test_session_dies_at_90_days_even_when_used_every_day` |
| 13 | No security headers | On every response (incl. errors and rejections): `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin` (not `no-referrer`: per the Fetch spec browsers then send `Origin: null` on every form POST, which the CSRF Origin check correctly refuses; `same-origin` still leaks nothing to other sites), minimal `Permissions-Policy`, and a CSP (below). | `test_security_headers_*`, `test_pages_carry_no_inline_script_or_handlers`, e2e `test_csp_journey.py` |
| 15 | Cookie `Secure` | One helper (`web/cookies.py`) for the session, acting-as and flash cookies. `SALES0_COOKIE_SECURE=auto|true|false` (default `auto`: https request, or `X-Forwarded-Proto: https` from a `SALES0_TRUSTED_PROXIES` peer, or an https `SALES0_PUBLIC_BASE_URL`). | unit `test_cookie_secure_*`, `test_every_cookie_helper_applies_the_decision`; `test_flash_cookie_*` |
| 16 | `/api/health` leaks details | Without a valid session (logins enabled) it returns only `{"status": "ok"}`; with a session, or in open mode (loopback Host only), the full payload. | `test_health_is_minimal_*`, `test_health_is_full_in_open_mode` |

Findings 2, 3, 5, 8, 12, 14, 17 belong to other fix groups.

Config risk fixed: `check_startup` refuses `SALES0_LINKEDIN_MODE=fixture` on a non-loopback `SALES0_HOST` (it would expose `/__fixtures/*` unauthenticated) - unit `test_fixture_linkedin_mode_is_refused_on_a_public_host`.

## Content-Security-Policy

```
default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline';
img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self';
form-action 'self' https://accounts.google.com; frame-ancestors 'none'
```

* No `'unsafe-inline'` for scripts: the inline `<script>` (digest copy button) and every inline handler
  (`onchange`, `onsubmit`, `onclick`) moved to `static/js/app.js` behind `data-autosubmit`, `data-confirm-submit`,
  `data-busy-submit`, `data-select-on-click` and a delegated `#copy-digest` handler. Inline `<style>` blocks (login,
  set-password) moved to `app.css`.
* htmx is configured by `<meta name="htmx-config">`: `includeIndicatorStyles:false` (it would inject a `<style>`),
  `allowEval:false`, `allowScriptTags:false`. Do not add `hx-on:*` or `hx-vals='js:...'` (they need eval).
* `style-src-attr 'unsafe-inline'` remains because four templates use `style="..."` attributes (progress/bar widths in
  `metrics/page.html`, `costs/page.html`). Inline *style elements* and stylesheets from other origins are still blocked.
  Removing the attributes would let this be dropped.
* `form-action` allows Google because "Connect Gmail" is a POST that redirects to the OAuth endpoint (derived from
  `SALES0_GOOGLE_AUTH_ENDPOINT`'s origin in the code).
* Verified in headless Chromium against a booted fixture-mode instance (every main page, an `hx-post` form, the
  confirm dialog, an auto-submitting select): zero violations, and an injected inline script is blocked.

## Vendored htmx

`static/vendor/htmx.min.js` is htmx **2.0.11** (the file reports `version:"2.0.11"`). Offline best effort: I know of no
published advisory against 2.0.x that applies to this app (it uses no `hx-on`, no `js:` expressions and escapes
user text in Jinja), but this could not be checked against a live advisory database from here. Before a release run
`npm view htmx.org versions` / the GitHub security advisories for `bigskysoftware/htmx` and bump if a newer 2.0.x
fixes anything. The CSP above (no inline script, no eval) and `allowEval:false` / `allowScriptTags:false` limit the
impact of a future htmx script-injection bug.

## Deployment checklist

* Behind a reverse proxy set `SALES0_TRUSTED_PROXIES` (else all visitors share one login-throttle bucket),
  `SALES0_PUBLIC_BASE_URL`, and `SALES0_ALLOWED_HOSTS` for any other name users type.
* Always set `SALES0_APP_PASSWORD` / account passwords on a server (open mode = loopback only, Host-checked).
* Keep `SALES0_LINKEDIN_MODE=live` on any non-loopback host (fixture mode is refused there).
