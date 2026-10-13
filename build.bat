@echo off
setlocal
cd /d "%~dp0"
set "PYBIN="
set "PYARGS="
if exist ".venv\Scripts\python.exe" set "PYBIN=.venv\Scripts\python.exe"
if "%PYBIN%"=="" (
  where py >nul 2>nul
  if not errorlevel 1 (
    set "PYBIN=py"
    set "PYARGS=-3"
  )
)
if "%PYBIN%"=="" (
  where python >nul 2>nul
  if not errorlevel 1 set "PYBIN=python"
)
if "%PYBIN%"=="" (
  echo [ERROR] Python 3.9+ not found on PATH.
  pause
  exit /b 1
)
"%PYBIN%" %PYARGS% scripts\build_release.py %*
if errorlevel 1 (
  echo [ERROR] Build failed. See messages above.
  pause
  exit /b 1
)
echo [OK] Build and smoke validation passed. Artifacts are in dist.
pause
exit /b 0
