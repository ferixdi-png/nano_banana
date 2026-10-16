import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from playwright.sync_api import sync_playwright

PROJECT_URL = os.environ.get("FLOW_PROJECT_URL", "https://flow.google.com/project/6b57709b-427c-4890-82fb-67a83106c1aa")
PROFILE = Path("/tmp/flow-remote-profile")
STATE = Path("/tmp/flow-storage-state.json")
state_lock = threading.Lock()
playwright = None
context = None
page = None

def browser_main():
    global playwright, context, page
    playwright = sync_playwright().start()
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        executable_path="/usr/bin/chromium",
        headless=False,
        viewport={"width": 1440, "height": 900},
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-first-run"],
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(PROJECT_URL, wait_until="domcontentloaded", timeout=120000)
    print("Remote Chromium launched. Complete Google sign-in in the VNC browser window.", flush=True)
    while True:
        time.sleep(2)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/save", "/health"):
            self.send_error(404)
            return
        if self.path == "/health":
            body = b"remote-browser-ready"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            with state_lock:
                if context is None:
                    raise RuntimeError("Browser context is not ready")
                context.storage_state(path=str(STATE))
                payload = STATE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition", 'attachment; filename="flow-storage-state.json"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            print("Flow session state exported by authenticated user.", flush=True)
        except Exception as exc:
            body = ("Unable to export browser session: " + str(exc)).encode()
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("state-export: " + (fmt % args), flush=True)

def server_main():
    HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()

if __name__ == "__main__":
    threading.Thread(target=browser_main, daemon=True).start()
    server_main()
