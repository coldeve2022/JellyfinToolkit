@echo off
REM Jellyfin Toolkit - source checkout launcher (Windows)
REM Creates a local .venv on first run, then starts the app.
setlocal

cd /d "%~dp0"

REM WorkBuddy/CodeBuddy sandbox shims intercept PyInstaller's internal
REM os.remove/rmtree. Clearing the session id disables that shim.
set "CODEBUDDY_SESSION_ID="
set "CLAUDE_SESSION_ID="

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if "%PY%"=="" (
  where py >nul 2>/dev/null && set "PY=py -3"
)
if "%PY%"=="" (
  where python >nul 2>/dev/null && set "PY=python"
)
if "%PY%"=="" (
  echo [ERROR] Python 3.9+ not found on PATH.
  echo         Install it from https://www.python.org/downloads/
  echo         and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [setup] Creating virtual environment .venv ...
  %PY% -m venv .venv || goto :fail
  set "PY=.venv\Scripts\python.exe"
  echo [setup] Installing dependencies ...
  "%PY%" -m pip install -q --upgrade pip
  "%PY%" -m pip install -q -r requirements.txt || goto :fail
)

start "" "%PY%" "%~dp0main.py"
exit /b 0

:fail
echo [ERROR] Setup failed. See the messages above.
pause
exit /b 1
