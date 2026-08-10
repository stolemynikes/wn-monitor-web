#!/usr/bin/env python3
"""Headful browser tasks driven from the web panel.

The CLI login waits on input(); a web UI can't press Enter in a terminal, so
this variant opens the browser and waits for a sentinel file that the panel
writes when the user clicks "I'm done".

    python browser_task.py login
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = PROJECT_DIR / "whatnot-profile"
PROFILE_BACKUP_DIR = PROJECT_DIR / "whatnot-profile-backup"
DONE_FILE = PROJECT_DIR / ".login_done"
RESULT_FILE = PROJECT_DIR / ".login_result"   # last outcome, shown in the panel
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
    login actually took, without launching anything.

    Delegates to profile_tools so this and the panel's badge can never disagree
    about where the cookie DB lives.
    """
    import profile_tools
    return profile_tools.session_state()["cookie_count"]


def chrome_owns_profile() -> bool:
    """True while any Chrome process still has our profile open.

    Chrome does not always stay in the process we launched: on Windows it
    regularly hands the profile to a relaunched chrome.exe and the original
    exits within a second. Treating that exit as "the user closed the window"
    would end the login before they had typed anything — and then nothing gets
    saved, which is exactly what "no profile yet" looks like afterwards.
    """
    try:
        import psutil
    except ImportError:
        return False
    marker = f"--user-data-dir={PROFILE_DIR}"
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if marker in (proc.info["cmdline"] or []):
                return True
        except Exception:      # incl. bare PermissionError from the OS
            continue
    return False


def close_chrome(proc) -> None:
    """Shut the login browser down and leave nothing holding the profile.

    Orphaned Chrome children keep file handles on whatnot-profile/, which is
    what makes the folder undeletable ("in use by another process") later.
    """
    try:
        import psutil
        tree = psutil.Process(proc.pid).children(recursive=True)
    except Exception:
        tree = []
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    for child in tree:
        try:
            child.kill()
        except Exception:
            pass
    for _ in range(20):        # a relaunched chrome.exe isn't in our tree
        if not chrome_owns_profile():
            return
        time.sleep(0.5)


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
    DONE_FILE.unlink(missing_ok=True)
    RESULT_FILE.unlink(missing_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    before = whatnot_cookie_count()

    chrome = find_google_chrome()
    if not chrome:
        return _report("Google Chrome not found — install it, or log in with "
                       "`python monitor.py login` instead.")

    print("Opening Chrome. Log in to Whatnot, then click \"I'm done\" in the "
          "panel (or just close the window).", flush=True)
    proc = subprocess.Popen(
        [chrome, f"--user-data-dir={PROFILE_DIR}",
         "--no-first-run", "--no-default-browser-check",
         "https://www.whatnot.com/login"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + LOGIN_TIMEOUT_SECONDS
    while time.time() < deadline:
        if DONE_FILE.exists():           # user pressed the panel button
            close_chrome(proc)
            break
        if proc.poll() is not None and not chrome_owns_profile():
            break                        # window really is gone
        time.sleep(1)
    DONE_FILE.unlink(missing_ok=True)

    time.sleep(2)  # let the cookie DB settle after shutdown
    after = whatnot_cookie_count()
    if after > 0:
        _report(f"Session saved — {after} whatnot cookies in the profile "
                f"(was {before}).")
    else:
        _report("No Whatnot cookies were saved — the login didn't complete. "
                "Run Login again and make sure you are signed in on "
                "whatnot.com before clicking \"I'm done\".")


def _report(message: str) -> None:
    """Say it on stdout and leave it where the panel can read it — the panel
    discards this process's output, so without the file the user is told
    nothing at all about why a login didn't take."""
    print(message, flush=True)
    try:
        RESULT_FILE.write_text(message, encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    if (sys.argv[1:2] or ["login"])[0] == "login":
        login()
    else:
        print(__doc__)
        sys.exit(1)
