#!/usr/bin/env python3
"""Headful browser tasks driven from the web panel.

The CLI login waits on input(); a web UI can't press Enter in a terminal, so
this variant opens the browser and waits for a sentinel file that the panel
writes when the user clicks "I'm done".

    python browser_task.py login
"""

import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = PROJECT_DIR / "whatnot-profile"
DONE_FILE = PROJECT_DIR / ".login_done"
LOGIN_TIMEOUT_SECONDS = 900  # 15 min, then close on its own


def login() -> None:
    from playwright.sync_api import sync_playwright

    DONE_FILE.unlink(missing_ok=True)
    print("Opening browser. Log in to Whatnot, then click 'I'm done' in the panel.",
          flush=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://www.whatnot.com/login", wait_until="domcontentloaded")
        except Exception as exc:
            print(f"could not load the login page: {exc}", flush=True)
        deadline = time.time() + LOGIN_TIMEOUT_SECONDS
        while time.time() < deadline and not DONE_FILE.exists():
            if not ctx.pages:  # user closed the window themselves
                break
            time.sleep(1)
        DONE_FILE.unlink(missing_ok=True)
        try:
            ctx.close()
        except Exception:
            pass
    print("Browser closed; session saved to the profile.", flush=True)


if __name__ == "__main__":
    if (sys.argv[1:2] or ["login"])[0] == "login":
        login()
    else:
        print(__doc__)
        sys.exit(1)
