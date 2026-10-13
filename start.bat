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
  echo [ERROR] Python 3.9+ not found. Install Python and add it to PATH.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  "%PYBIN%" %PYARGS% -m venv .venv || goto :fail
  set "PYBIN=.venv\Scripts\python.exe"
  set "PYARGS="
)
"%PYBIN%" %PYARGS% -c "import PySide6" >nul 2>nul
if errorlevel 1 (
  "%PYBIN%" %PYARGS% -m pip install -r requirements.txt || goto :fail
)
start "" "%PYBIN%" %PYARGS% "%~dp0main.py"
exit /b 0
:fail
echo [ERROR] Setup failed. See the messages above.
pause
exit /b 1
