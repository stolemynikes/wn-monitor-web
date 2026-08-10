@echo off
REM Double-click this to install (first time) and open the Whatnot Radar panel.
REM Safe to run again any time - it only does the setup that's still missing.
cd /d "%~dp0"

REM --- find a usable Python -------------------------------------------------
set PY=
for %%C in (python py) do (
  if not defined PY (
    %%C -c "import sys; sys.exit(sys.version_info < (3,11))" >nul 2>&1 && set PY=%%C
  )
)
if not defined PY (
  echo.
  echo   Python 3.11 or newer is required.
  echo   Get it from https://www.python.org/downloads/
  echo   Tick "Add Python to PATH" during install, then run this again.
  echo.
  pause
  exit /b 1
)

REM --- environment ----------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   First run - setting up. This takes a few minutes, only once.
  %PY% -m venv .venv || goto fail
)

REM --- dependencies (a fast no-op once they're installed) -------------------
".venv\Scripts\python.exe" -c "import fastapi, playwright, psutil, qrcode" >nul 2>&1
if errorlevel 1 (
  echo   Installing components...
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto fail
)

REM --- a browser to drive ---------------------------------------------------
REM Real Chrome is preferred, so only pull Playwright's ~150 MB Chromium
REM when Chrome isn't already installed.
if not exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
 if not exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" (
  if not exist "%USERPROFILE%\AppData\Local\ms-playwright" (
   echo   Downloading a browser ^(~150 MB, one time^)...
   ".venv\Scripts\playwright.exe" install chromium || goto fail
  )
 )
)

REM --- settings file --------------------------------------------------------
if not exist "config.json" copy /y config.example.json config.json >nul

echo.
echo   Starting the panel - leave this window open.
".venv\Scripts\python.exe" web.py
pause
exit /b 0

:fail
echo.
echo   Setup failed. Check your internet connection and try again.
pause
exit /b 1
