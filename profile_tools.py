#!/usr/bin/env python3
"""Browser-profile inspection and cleanup.

Two very different operations, deliberately kept separate:
  clear_cache()      — safe, disposable, keeps you logged in
  clear_site_data()  — destructive, logs you out of Whatnot
"""

import shutil
import sqlite3
import tempfile
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

# The actual session. Deleting these means logging in again.
SITE_DATA_PATHS = [
    "Default/Cookies", "Default/Cookies-journal", "Default/Local Storage",
    "Default/Session Storage", "Default/IndexedDB", "Default/Service Worker",
]


def _size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def sizes() -> dict:
    """Profile size split into what's disposable and what isn't."""
    cache = sum(_size(PROFILE_DIR / p) for p in CACHE_PATHS)
    total = _size(PROFILE_DIR)
    return {"total_bytes": total, "cache_bytes": cache,
            "session_bytes": sum(_size(PROFILE_DIR / p) for p in SITE_DATA_PATHS)}


def _delete(paths) -> int:
    freed = 0
    for rel in paths:
        target = PROFILE_DIR / rel
        freed += _size(target)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink(missing_ok=True)
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
    return freed


def session_state() -> dict:
    """Whether a Whatnot session looks present, read straight from the cookie
    DB — far cheaper than launching a browser to find out."""
    cookies = PROFILE_DIR / "Default" / "Cookies"
    if not cookies.exists():
        return {"logged_in": False, "cookie_count": 0,
                "detail": "no profile yet — run Login"}
    tmp = Path(tempfile.mkdtemp()) / "Cookies"  # copy: the live DB may be locked
    try:
        shutil.copy(cookies, tmp)
        with sqlite3.connect(tmp) as con:
            n = con.execute(
                "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%whatnot%'"
            ).fetchone()[0]
    except (OSError, sqlite3.Error) as exc:
        return {"logged_in": False, "cookie_count": 0, "detail": f"unreadable: {exc}"}
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    return {"logged_in": n > 0, "cookie_count": n,
            "detail": f"{n} whatnot.com cookies" if n else "no Whatnot cookies — run Login"}
