@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM ===========================================================
REM  Launcher for Jellyfin Toolkit (double-click this file).
REM
REM  Why not double-click main.pyw:
REM    .pyw uses a Windows file association, so which python runs
REM    depends on the registry. With several Pythons installed it
REM    may pick one without PySide6 and fail silently.
REM  This script picks the interpreter itself, verifies PySide6,
REM  and keeps the window open on error so you can see why.
REM
REM  NOTE: this file must stay CRLF + pure ASCII. A .bat with
REM  LF-only line endings makes cmd.exe merge lines and report
REM  "not recognized as an internal or external command".
REM ===========================================================

set "PYBIN="
set "PYARGS="

if exist "%~dp0.venv\Scripts\python.exe" (
  set "PYBIN=%~dp0.venv\Scripts\python.exe"
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
  echo.
  echo   [ERROR] No Python 3.10+ found.
  echo           Install Python and run this file again.
  echo.
  pause
  exit /b 1
)

echo.
echo   Interpreter: %PYBIN% %PYARGS%
"%PYBIN%" %PYARGS% -c "import sys, PySide6; print('  Python', sys.version.split()[0], '/ PySide6', PySide6.__version__)"
if errorlevel 1 (
  echo.
  echo   [ERROR] This Python has no usable PySide6.
  echo           Fix: "%PYBIN%" %PYARGS% -m pip install -r requirements.txt
  echo           Or delete the .venv folder and run start.bat again.
  echo.
  pause
  exit /b 1
)

if /i "%~1"=="--check" (
  echo   [check] OK - dependencies are fine.
  exit /b 0
)

echo   Starting...
"%PYBIN%" %PYARGS% main.py
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo   [EXIT] code %CODE% - the output above is the reason.
  echo          Log: %~dp0data\startup-error.log
  echo.
  pause
)
exit /b %CODE%
