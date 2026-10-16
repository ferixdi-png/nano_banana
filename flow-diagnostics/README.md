# Flow Diagnostics — Ferixdi

A minimal Chromium Manifest V3 extension for diagnosing visible Google Flow page states.

## What it does
- Detects visible text associated with a Flow warning, sign-in requirement, or generic page error.
- Displays a small on-page diagnostic card with a short explanation.
- Uses no external network requests and does not collect or transmit page content.

## What it deliberately does not do
- It does not hide or remove Flow's own warnings.
- It does not modify requests, cookies, account/session identifiers, or browser fingerprints.
- It does not click through checks or retry rejected actions.

## Install for manual diagnosis
1. Open `chrome://extensions` in Chromium.
2. Enable Developer mode.
3. Choose **Load unpacked**.
4. Select this `flow-diagnostics` directory.

This is a diagnostic aid, not a fix for a server-side rejection. If Flow refuses a generation, stop repeated attempts and inspect the page and bot logs.
