#!/bin/bash
# Double-click this on macOS to open the Whatnot Radar panel.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Setup hasn't been run yet. See README.md, Part 1."
  read -r -p "Press Enter to close."
  exit 1
fi
exec .venv/bin/python web.py
