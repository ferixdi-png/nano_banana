"""Export Playwright storage state from the already logged-in local Chrome.

Run this on the PC where Chrome is open with remote debugging on port 9222.
The output contains authentication cookies and must be treated like a password.
Never commit or send the resulting JSON file to chat.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

CDP_URL = "http://127.0.0.1:9222"
OUTPUT = Path("flow-storage-state.json")


def main():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found. Start the dedicated Chrome with --remote-debugging-port=9222.")
        context = browser.contexts[0]
        pages = context.pages
        if not any("flow.google.com" in page.url or "labs.google/fx/tools/flow" in page.url for page in pages):
            raise RuntimeError("Open Google Flow in the dedicated Chrome and make sure you are signed in.")
        context.storage_state(path=str(OUTPUT))
        print(f"Session state exported to {OUTPUT.resolve()}")
        print("WARNING: this file contains login cookies. Do not commit it or share it publicly.")


if __name__ == "__main__":
    main()
