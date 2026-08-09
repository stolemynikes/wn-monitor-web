@echo off
REM Double-click this on Windows to open the Whatnot Radar panel.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Setup hasn't been run yet. See README.md, Part 1.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" web.py
pause
