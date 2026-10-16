# Nano Banana Flow Telegram Bot

Telegram bot that submits prompts to Google Flow, uses Nano Banana 2.1 in 9:16 / x1 mode, downloads the 1K result, and sends it back to the allowed Telegram user.

## Render deployment

This repository includes a Docker-based Render Background Worker. The worker needs these environment variables:

- `TELEGRAM_BOT_TOKEN`: bot token from BotFather.
- `ALLOWED_USER_ID`: Telegram numeric user ID.
- `FLOW_PROJECT_URL`: URL of the Google Flow project to use.
- `FLOW_STORAGE_STATE_B64`: optional base64-encoded Playwright storage-state JSON exported from a logged-in browser.
- `FLOW_GOOGLE_EMAIL`, `FLOW_GOOGLE_PASSWORD`, `FLOW_GOOGLE_TOTP_SECRET`: optional Google sign-in credentials for request-time login when Flow rejects the saved session. Store them only in Render Environment, never in repository files.

The Render service has a persistent disk mounted at `/var/data` to preserve the browser session state between restarts. The bot checks the Flow project on every generation request and reuses an accepted session. If Flow redirects away from the project, it attempts a normal Google sign-in using the optional Render variables, then saves the refreshed browser session. Google security challenges or suspicious-activity checks are not bypassed; if Google requires one, the request stops with an actionable error. Any exported browser state contains Google authentication cookies and must be treated as a secret.

### Export the local session

1. On the PC, open the dedicated Chrome that is already logged into Google Flow with remote debugging enabled on port 9222.
2. Install the project dependencies locally if needed: `python -m pip install -r requirements.txt` and `python -m playwright install chromium`.
3. Run `python export_flow_session.py` in the project directory.
4. In PowerShell, copy the encoded state to the clipboard without printing it:
   `[Convert]::ToBase64String([IO.File]::ReadAllBytes("flow-storage-state.json")) | Set-Clipboard`
5. In Render, add the clipboard contents as the secret value for `FLOW_STORAGE_STATE_B64`. Do not paste it into GitHub or a chat.
6. Set `FLOW_PROJECT_URL` to the exact URL of the project in Google Flow, then deploy.

**Important:** Google may reject copied cookies or require a fresh sign-in / device verification. A successful deploy does not guarantee that Flow will accept the exported session in a headless cloud browser. Check Render logs and test a prompt. If Google invalidates the session, refresh it locally and repeat the export.

## Local run

For local Chrome CDP mode, set `CDP_URL=http://127.0.0.1:9222` in the local `.env`. Do not set `CDP_URL` in Render. Keep `.env`, `.flow-profile`, `flow-storage-state.json`, and any encoded session values out of Git.

## Settings

The bot checks that Nano Banana 2.1 is selected and sets image mode, 9:16, and x1. It chooses `1K Исходный размер` when downloading. Temporary failures are retried up to three times with a 10-second delay.
