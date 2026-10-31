@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto :missing
".venv\Scripts\python.exe" "%~dp0tools\dev\run_profile.py" %*
set "JT_EXIT=%ERRORLEVEL%"
if not "%JT_EXIT%"=="0" if not defined JELLYFIN_TOOLKIT_NO_PAUSE pause
exit /b %JT_EXIT%
:missing
echo [ERROR] Project Python was not found. Run start.bat to prepare the environment.
if not defined JELLYFIN_TOOLKIT_NO_PAUSE pause
exit /b 1
