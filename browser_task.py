#!/usr/bin/env python3
"""Headful browser tasks driven from the web panel.

The CLI login waits on input(); a web UI can't press Enter in a terminal, so
this variant opens the browser and waits for a sentinel file that the panel
writes when the user clicks "I'm done".

    python browser_task.py login
"""

import shutil
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = PROJECT_DIR / "whatnot-profile"
PROFILE_BACKUP_DIR = PROJECT_DIR / "whatnot-profile-backup"
DONE_FILE = PROJECT_DIR / ".login_done"
LOGIN_TIMEOUT_SECONDS = 900  # 15 min, then close on its own


def find_google_chrome() -> str | None:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/google-chrome",
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    ]
    for path in candidates:
        if Path(path).exists():
            return str(path)
    for command in ("google-chrome", "google-chrome-stable", "chrome"):
        resolved = shutil.which(command)
        if resolved:
            return resolved
    return None


def build_browser_launch_kwargs(user_data_dir: str | Path, *, headless: bool = False):
    kwargs = {
        "user_data_dir": str(user_data_dir),
        "headless": headless,
        "chromium_sandbox": True,
    }
    chrome_path = find_google_chrome()
    if chrome_path:
        kwargs["executable_path"] = chrome_path
    return kwargs


def backup_profile(source_dir: Path = PROFILE_DIR, backup_dir: Path = PROFILE_BACKUP_DIR) -> bool:
    if not source_dir.exists():
        return False
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
    shutil.copytree(source_dir, backup_dir)
    return True


def whatnot_cookie_count() -> int:
    """How many whatnot.com cookies the profile holds — used to tell whether a
    login actually took, without launching anything."""
    import sqlite3
    import tempfile
    src = PROFILE_DIR / "Default" / "Cookies"
    if not src.exists():
        return 0
    tmp = Path(tempfile.mkdtemp()) / "c"
    try:
        shutil.copy(src, tmp)
        with sqlite3.connect(tmp) as con:
            return con.execute("SELECT COUNT(*) FROM cookies "
                               "WHERE host_key LIKE '%whatnot%'").fetchone()[0]
    except Exception:
        return 0
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def login() -> None:
    """Open a plain, human-driven Chrome on the tool's profile so you can sign
    in yourself.

    Deliberately NOT Playwright. Whatnot's protection tolerates automated
    anonymous browsing but challenges an automated browser at the login step,
    so driving this with Playwright — even using Chrome's own binary — walks
    into the "just a moment" loop. Launching Chrome as an ordinary process
    means a real person really is doing the login; the radar then reuses the
    session that produced.
    """
    import subprocess

    DONE_FILE.unlink(missing_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    before = whatnot_cookie_count()

    chrome = find_google_chrome()
    if not chrome:
        print("Google Chrome not found — install it, or log in with "
              "`python monitor.py login` instead.", flush=True)
        return

    print("Opening Chrome. Log in to Whatnot, then click \"I'm done\" in the "
          "panel (or just close the window).", flush=True)
    proc = subprocess.Popen(
        [chrome, f"--user-data-dir={PROFILE_DIR}",
         "--no-first-run", "--no-default-browser-check",
         "https://www.whatnot.com/login"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + LOGIN_TIMEOUT_SECONDS
    while time.time() < deadline:
        if proc.poll() is not None:      # user closed Chrome themselves
            break
        if DONE_FILE.exists():           # user pressed the panel button
            proc.terminate()             # graceful, so Chrome flushes cookies
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
            break
        time.sleep(1)
    DONE_FILE.unlink(missing_ok=True)

    time.sleep(1)  # let the cookie DB settle after shutdown
    after = whatnot_cookie_count()
    if after > 0:
        print(f"Session saved — {after} whatnot cookies in the profile "
              f"(was {before}).", flush=True)
    else:
        print("No Whatnot cookies found — the login didn't complete. "
              "Try again and make sure you're signed in before closing.",
              flush=True)


if __name__ == "__main__":
    if (sys.argv[1:2] or ["login"])[0] == "login":
        login()
    else:
        print(__doc__)
        sys.exit(1)
