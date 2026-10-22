@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM ───────────────────────────────────────────────────────────
REM  依次尝试：项目自带 .venv → 常见安装位置 → py -3 → PATH 的 python
REM  **不再直接用 where python**：本机 PATH 第一位可能是某个工具自带的
REM  隔离运行时（没装 PySide6），命中它就会启动失败。
REM ───────────────────────────────────────────────────────────
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

start "" "%PYBIN%" %PYARGS% "%~dp0main.py"
exit /b 0

:fail
echo.
echo [ERROR] Setup failed. The messages above are the reason.
echo         Double-click 启动.bat to see the details in a console window.
pause
exit /b 1
