#!/bin/bash
# Double-click this to install (first time) and open the Whatnot Radar panel.
# Safe to run again any time — it only does the setup that's still missing.
cd "$(dirname "$0")" || exit 1

say() { printf "\n  %s\n" "$1"; }
die() { printf "\n  %s\n\n  Press Enter to close.\n" "$1"; read -r _; exit 1; }

# --- environment ----------------------------------------------------------
# Try each Python until one produces a WORKING venv. Version alone isn't
# enough: some installs (e.g. a Homebrew python whose pyexpat is linked against
# a newer libexpat than the OS ships) pass the version check but fail at
# ensurepip. So build it, then prove pip runs before accepting it.
if [ ! -x .venv/bin/python ]; then
  say "First run — setting up. This takes a few minutes, only once."
  for c in python3.13 python3.12 python3.11 python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import sys; sys.exit(sys.version_info < (3,11))' 2>/dev/null || continue
    rm -rf .venv
    if "$c" -m venv .venv >/dev/null 2>&1 \
       && .venv/bin/python -m pip --version >/dev/null 2>&1; then
      break
    fi
    printf "  (%s couldn't build a working environment, trying another)\n" "$c"
    rm -rf .venv
  done
fi

# Last resort: uv ships its own self-contained Python and sidesteps a broken
# system one entirely.
if [ ! -x .venv/bin/python ] && command -v uv >/dev/null 2>&1; then
  say "Using uv to build the environment..."
  uv venv --python 3.12 --seed .venv >/dev/null 2>&1
fi

[ -x .venv/bin/python ] || die "Couldn't build a working Python environment.
  Install Python 3.11+ from https://www.python.org/downloads/
  (the python.org build is the most reliable), then try again."

# --- dependencies (a fast no-op once they're installed) -------------------
if ! .venv/bin/python -c 'import fastapi, playwright, psutil, qrcode' 2>/dev/null; then
  say "Installing components..."
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt \
    || die "Install failed. Check your internet connection and try again."
fi

# --- a browser to drive ---------------------------------------------------
# Real Chrome is preferred (and is what the login uses), so only pull
# Playwright's ~150 MB Chromium when Chrome isn't already installed.
if [ ! -d "/Applications/Google Chrome.app" ] \
   && [ ! -d "$HOME/Library/Caches/ms-playwright" ]; then
  say "Downloading a browser (~150 MB, one time)..."
  .venv/bin/playwright install chromium || die "Browser download failed."
fi

# --- settings file --------------------------------------------------------
[ -f config.json ] || cp config.example.json config.json

say "Starting the panel — leave this window open."
exec .venv/bin/python web.py
