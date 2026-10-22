@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM ===========================================================
REM  Setup + start. Prefer the other .bat (the launcher) for normal
REM  use: it keeps the console open on failure. This one installs
REM  dependencies first if they are missing.
REM
REM  Interpreter order: project .venv -> common install paths ->
REM  py -3 -> PATH python. Do NOT rely on "where python" first:
REM  on some machines PATH starts with a bundled runtime that has
REM  no PySide6.
REM
REM  NOTE: keep this file CRLF + pure ASCII (see the launcher).
REM ===========================================================

set "PYBIN="
set "PYARGS="

if exist ".venv\Scripts\python.exe" (
  set "PYBIN=.venv\Scripts\python.exe"
  goto :verify
)
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
  set "PYBIN=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  goto :verify
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  set "PYBIN=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  goto :verify
)
if exist "%ProgramFiles%\Python313\python.exe" (
  set "PYBIN=%ProgramFiles%\Python313\python.exe"
  goto :verify
)
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  set "PYBIN=py"
  set "PYARGS=-3"
  goto :verify
)
where python >nul 2>nul
if not errorlevel 1 set "PYBIN=python"

:verify
if "%PYBIN%"=="" (
  echo [ERROR] Python 3.10+ not found. Install Python and try again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [SETUP] Creating .venv with %PYBIN% ...
  "%PYBIN%" %PYARGS% -m venv .venv || goto :fail
  set "PYBIN=.venv\Scripts\python.exe"
  set "PYARGS="
)

"%PYBIN%" %PYARGS% -c "import PySide6" >nul 2>nul
if errorlevel 1 (
  echo [SETUP] Installing dependencies with %PYBIN% ...
  "%PYBIN%" %PYARGS% -m pip install -r requirements.txt || goto :fail
)

if /i "%~1"=="--check" (
  "%PYBIN%" %PYARGS% -c "import sys, PySide6; print('[check]', sys.version.split()[0], '/ PySide6', PySide6.__version__)"
  exit /b 0
)

start "" "%PYBIN%" %PYARGS% "%~dp0main.py"
exit /b 0

:fail
echo.
echo [ERROR] Setup failed. The messages above are the reason.
echo         Double-click the launcher .bat to see details in a console.
pause
exit /b 1
