# Gmail setup (one-time, about 10 minutes)

sales0 talks to Gmail through your own Google Cloud OAuth client. Nothing is sent unless a
later phase's outreach service sends a draft you approved.

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
Go to sales0 (unsafe)**. It is your own app. Requested access: create and send drafts
(`gmail.compose`) and your email address (`openid email`). sales0 cannot read your inbox.

**Testing-mode refresh tokens expire after 7 days** (External + Testing). Either re-run
`google-auth` weekly, or publish the app to production (External; stays "unverified" but
tokens persist), or use an Internal app on Workspace.

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
