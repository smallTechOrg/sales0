# Gmail setup (one-time, about 10 minutes)

sales0 talks to Gmail through your own Google Cloud OAuth client. Nothing is sent unless the
outreach service sends a draft a person approved.

1. Open https://console.cloud.google.com/ and sign in with the Gmail account that will send mail.
2. Project picker (top bar) -> **New project** -> name it `sales0` -> **Create**, then select it.
3. **APIs & Services -> Library** -> search "Gmail API" -> **Enable**.
4. **APIs & Services -> OAuth consent screen** (may be called "Google Auth platform"):
   - Google Workspace account (your own domain): User type **Internal**.
   - Personal @gmail.com: User type **External**, publishing status **Testing**, and under
     **Test users** add your own Gmail address.
   - App name `sales0`, support email and developer contact = your address. Scopes can stay empty
     (the app asks for them at sign-in).
5. **APIs & Services -> Credentials -> Create credentials -> OAuth client ID** -> Application type
   **Desktop app** -> name `sales0` -> **Create** -> **Download JSON**.
6. Save that file as `<repo>/data/google_client_secret.json`.
   Live copy: `/Users/sai/Workspace/Code/sales0-run/data/google_client_secret.json`
   (or point `SALES0_GOOGLE_CLIENT_SECRET_PATH` elsewhere). `data/` is gitignored.
7. Run `uv run python -m sales0.cli google-auth`. Your browser opens Google's consent screen on a
   local loopback port; approve. The token is saved to `data/google_token.json` (mode 600).

Expect the **"Google hasn't verified this app"** screen (External/Testing): click **Advanced ->
Go to sales0 (unsafe)**. It is your own app. Requested access: `gmail.modify` (drafts, send, trash for test cleanup, and read-only
reply tracking of threads you sent) and your email address (`openid email`).

**Testing-mode refresh tokens expire after 7 days** (External + Testing). Either re-run
`google-auth` weekly, or publish the app to production (External; stays "unverified" but
tokens persist), or use an Internal app on Workspace.

## Hosted deployment: Web application client

The Desktop client above only allows loopback redirects, so it cannot be used when sales0 runs on
a server. For a hosted install:

1. In step 5 choose Application type **Web application**.
2. Under **Authorized redirect URIs** add exactly `<SALES0_PUBLIC_BASE_URL>/oauth/google/callback`,
   for example `https://sales.example.com/oauth/google/callback` (scheme, host, port and path must match;
   no trailing slash). Download the JSON (it has a `web` block) to `SALES0_GOOGLE_CLIENT_SECRET_PATH`.
3. Set `SALES0_PUBLIC_BASE_URL=https://sales.example.com` and restart.
4. Each person opens **Team** on that URL and presses **Connect my Gmail**. The Team page shows the exact
   redirect URI to register. If it is not listed in the JSON's `redirect_uris`, or a Desktop client is used with
   a public URL, Connect stops with an error naming the URI.

For a Workspace domain keep the consent screen **Internal**. External users need Google's app verification.

## Verify (never sends)

```
uv run python -m sales0.cli gmail-check
```
Creates a draft to your own address with a PDF, reads it back, deletes it, prints `gmail OK ...`.

## Optional: one real send, to yourself only

```
uv run python -m sales0.cli gmail-send-test --yes-send
```
Sends one clearly labelled test email to the authenticated address; refuses any other recipient.

## Errors

| Message | Fix |
|---|---|
| client secret not found at ... | Step 6: save the JSON at the printed path |
| token revoked or has expired | re-run `google-auth` (step 7) |
| lacks the scope / insufficient authentication scopes | re-run `google-auth` (scopes changed) |
| 403 access not configured | enable the Gmail API (step 3) |
