# Gmail integration (Phase 3D)

Adapter, OAuth and operator checks for sending outreach through the user's own Gmail.
Code: `src/sales0/integrations/gmail/`. Setup: `docs/gmail-setup.md`. No DB, no web routes.

## Behaviour
- **Auth**: installed-app (Desktop) OAuth via `python -m sales0.cli google-auth`,
  loopback redirect on a free port, offline access. Token JSON at `SALES0_GOOGLE_TOKEN_PATH`
  (default `data/google_token.json`, chmod 600, gitignored). Client secret at
  `SALES0_GOOGLE_CLIENT_SECRET_PATH` (default `data/google_client_secret.json`). Refresh is
  transparent (httpx POST to the token endpoint, access token cached in memory).
- **Scopes** (`auth.SCOPES`): `gmail.compose`, `openid`, `userinfo.email`. Reply tracking adds
  `gmail.readonly` there and in `REQUIRED_FOR["list_sent_since"]`; old tokens then fail with
  "run google-auth again".
- **Client** (`GmailClient` protocol; `RealGmailClient`, `FakeGmailClient`): `profile_email`,
  `create_draft(to, cc, subject, text_body, html_body, attachments)`, `get_draft`, `delete_draft`,
  `send_draft(ApprovedMessage)`, `get_thread(thread_id)` and
  `list_threads_with_new_messages(since, limit)` (read-only, `format=metadata`: headers + snippet,
  no body; `gmail.modify` covers the read scope, `REQUIRED_FOR["read"]`).
- **MIME** is built locally (`mime.py`): UTF-8 subjects, text+HTML alternative, attachments,
  25 MB limit with a clear error, plain `local@domain` addresses only, CR/LF/control characters
  rejected in addresses, subject and filenames.
- **Errors**: 401 -> refresh once, then `GmailAuthError` (re-run google-auth); revoked/expired
  refresh token -> `GmailAuthError`; 403 insufficient scope -> `GmailScopeError`; 404 ->
  `GmailNotFound`; 429/rate-limit 403/5xx -> retries with exponential backoff (Retry-After honoured),
  then `GmailTransientError`. A send is never retried on 5xx/network error (outcome unknown).

## Safety (binding)
1. The adapter never sends implicitly; the only send path is `send_draft(ApprovedMessage)`.
2. `ApprovedMessage` (frozen: `draft_id`, `sha256`, `approved_by`, `approved_at`) binds to
   `DraftContent.sha256`, a canonical hash of recipients, subject, bodies and attachment bytes
   (not the raw bytes, which Gmail rewrites on storage).
3. `send_draft` re-fetches the draft, recomputes the hash, and raises `ApprovalMismatch`
   without sending on any difference (edited after approval, wrong draft id).
4. The outreach service (a later phase) is the ONLY production caller of `send_draft`.
5. `gmail-check` never sends. `gmail-send-test --yes-send` sends one labelled email to the
   authenticated address only; other recipients need the undocumented `--allow-external`.

## Not verified without real Google
Consent flow, Gmail's draft re-serialisation vs. the canonical hash (expected to match; if Gmail
alters body whitespace beyond trailing newlines `gmail-send-test` fails closed with a mismatch).
