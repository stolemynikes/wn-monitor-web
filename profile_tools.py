#!/usr/bin/env python3
"""Browser-profile inspection and cleanup.

Two very different operations, deliberately kept separate:
  clear_cache()      — safe, disposable, keeps you logged in
  clear_site_data()  — destructive, logs you out of Whatnot
"""

import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = PROJECT_DIR / "whatnot-profile"

# Regenerated automatically by Chromium; deleting them costs only a slightly
# slower first page load. A month of livestream loads can push these past 1 GB.
CACHE_PATHS = [
    "Default/Cache", "Default/Code Cache", "Default/GPUCache",
    "Default/DawnWebGPUCache", "Default/DawnGraphiteCache",
    "GraphiteDawnCache", "GPUPersistentCache", "ShaderCache",
]

# Where the cookie DB lives depends on the build: Chrome moved it under
# Default/Network, Playwright's Chromium still writes Default/Cookies. The
# login uses real Chrome and the radar may use either, so always check both —
# looking in one place only is why a good session reported "no profile yet".
COOKIE_PATHS = [
    "Default/Network/Cookies", "Default/Network/Cookies-journal",
    "Default/Cookies", "Default/Cookies-journal",
]

# The actual session. Deleting these means logging in again.
SITE_DATA_PATHS = COOKIE_PATHS + [
    "Default/Local Storage", "Default/Session Storage",
    "Default/IndexedDB", "Default/Service Worker",
]


def _size(path: Path) -> int:
    """Bytes under path, tolerating files that vanish mid-walk.

    We measure a profile a live Chrome is writing to: it evicts cache entries
    constantly, so a file listed by rglob is routinely gone by the time we stat
    it. That raced with the panel's 2.5s poll and took /api/status down with a
    FileNotFoundError. A size that is a few KB stale is fine; a 500 is not.
    """
    total = 0
    try:
        if not path.exists():
            return 0
        if path.is_file():
            return path.stat().st_size
        for f in path.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue          # deleted, or a symlink we may not follow
    except OSError:
        pass
    return total


_SIZE_CACHE: dict = {"at": 0.0, "value": None}
SIZE_CACHE_SECONDS = 30


def sizes(max_age: float = SIZE_CACHE_SECONDS) -> dict:
    """Profile size split into what's disposable and what isn't.

    Cached: this walks every file in a profile that routinely passes a
    gigabyte, and the panel asks for status every 2.5 seconds.
    """
    if _SIZE_CACHE["value"] is not None and time.time() - _SIZE_CACHE["at"] < max_age:
        return _SIZE_CACHE["value"]
    value = {
        "total_bytes": _size(PROFILE_DIR),
        "cache_bytes": sum(_size(PROFILE_DIR / p) for p in CACHE_PATHS),
        "session_bytes": sum(_size(PROFILE_DIR / p) for p in SITE_DATA_PATHS),
    }
    _SIZE_CACHE.update(at=time.time(), value=value)
    return value


def _delete(paths) -> int:
    freed = 0
    for rel in paths:
        target = PROFILE_DIR / rel
        freed += _size(target)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink(missing_ok=True)
    _SIZE_CACHE["value"] = None   # the panel must show the new size at once
    return freed


def clear_cache() -> int:
    """Delete disposable caches. Returns bytes freed. Session is untouched."""
    return _delete(CACHE_PATHS)


def clear_site_data() -> int:
    """Delete cookies and site storage — LOGS YOU OUT. Returns bytes freed."""
    return _delete(SITE_DATA_PATHS)


def reset_profile() -> int:
    """Throw the profile away and start empty. Returns bytes freed.

    Measured 2026-08-10: when Cloudflare starts serving the endless "just a
    moment" challenge, it's the profile's site data that's flagged — the same
    browser on the same connection loads fine once cookies/storage are gone.
    So the only reset that resets anything is an empty profile plus a fresh
    login. Never reseed from a backup: every backup is a copy of the profile
    that got flagged, so restoring one restores the flag.
    """
    freed = _size(PROFILE_DIR)
    shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _SIZE_CACHE["value"] = None
    return freed


def cookie_db() -> Path | None:
    """The profile's cookie database, wherever this browser build keeps it."""
    for rel in COOKIE_PATHS:
        if rel.endswith("-journal"):
            continue
        candidate = PROFILE_DIR / rel
        # is_file, not exists: opening a directory raises PermissionError on
        # Windows rather than IsADirectoryError, which reads as a lock.
        if candidate.is_file():
            return candidate
    return None


# Last count we managed to read. Windows keeps the cookie DB locked while the
# browser has it open, so "can't read it right now" must not be reported as
# "logged out" — that flips the badge and trips the start gate on a profile
# that is perfectly fine.
SEEN_PATH = PROJECT_DIR / ".session_seen"


def _remember(count: int) -> None:
    try:
        SEEN_PATH.write_text(str(count), encoding="utf-8")
    except OSError:
        pass


def _last_known() -> int:
    try:
        return int(SEEN_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _count_whatnot_cookies(db: Path) -> int:
    """Cookies for whatnot.com, read without disturbing the live database.

    Tries the file in place first: SQLite opens read-only/immutable with
    permissive share flags and often succeeds where a plain copy is refused.
    Falls back to copying, which works when the file is merely busy.
    """
    query = "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%whatnot%'"
    try:
        uri = f"file:{db.as_posix()}?immutable=1"
        con = sqlite3.connect(uri, uri=True)
        try:
            return con.execute(query).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        pass
    tmp = Path(tempfile.mkdtemp()) / "Cookies"
    try:
        shutil.copy(db, tmp)
        con = sqlite3.connect(tmp)
        try:
            return con.execute(query).fetchone()[0]
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def session_state() -> dict:
    """Whether a Whatnot session looks present, read straight from the cookie
    DB — far cheaper than launching a browser to find out."""
    cookies = cookie_db()
    if cookies is None:
        # Distinguish the two: "never logged in" and "the browser opened but
        # the login never completed" need different things from the user.
        detail = ("no profile yet — run Login" if not PROFILE_DIR.exists()
                  else "profile exists but holds no cookies — the login didn't "
                       "finish, run Login again")
        return {"logged_in": False, "cookie_count": 0, "detail": detail}
    try:
        n = _count_whatnot_cookies(cookies)
    except (OSError, sqlite3.Error) as exc:
        if (last := _last_known()):
            return {"logged_in": True, "cookie_count": last, "locked": True,
                    "detail": f"{last} whatnot.com cookies — the browser has "
                              "the file open, so this is the last known count"}
        return {"logged_in": False, "cookie_count": 0, "locked": True,
                "detail": "the cookie file is in use by another program — stop "
                          f"the radar, then reload ({exc.__class__.__name__})"}
    _remember(n)
    return {"logged_in": n > 0, "cookie_count": n, "locked": False,
            "detail": f"{n} whatnot.com cookies" if n else "no Whatnot cookies — run Login"}
