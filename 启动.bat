@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ═══════════════════════════════════════════════════════════════
REM  启动 Jellyfin Toolkit（推荐用这个双击启动）
REM
REM  为什么不直接双击 main.pyw：
REM    .pyw 走 Windows 文件关联，用哪个 python 由注册表决定 —— 机器上装了
REM    多个 Python 时，可能指向一个没装 PySide6 的解释器，双击就只弹
REM    "Application Error 0xc0000142"，什么也看不到。
REM  这个脚本按固定顺序找解释器，并且**先验证 PySide6**，出错直接说原因。
REM ═══════════════════════════════════════════════════════════════

set "PY="
set "PYARG="

if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY=%~dp0.venv\Scripts\python.exe"
  goto :check
)
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
  set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  goto :check
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  goto :check
)
if exist "%ProgramFiles%\Python313\python.exe" (
  set "PY=%ProgramFiles%\Python313\python.exe"
  goto :check
)
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  set "PY=py"
  set "PYARG=-3"
  goto :check
)

echo.
echo  [错误] 没找到可用的 Python 3。
echo         请安装 Python 3.10+（低于 3.10 不支持），或用 start.bat 自动建环境。
echo.
pause
exit /b 1

:check
echo.
echo  使用解释器：%PY% %PYARG%
"%PY%" %PYARG% -c "import PySide6,sys;print('  Python',sys.version.split()[0],'/ PySide6',PySide6.__version__)"
if errorlevel 1 (
  echo.
  echo  [错误] 这个解释器没有可用的 PySide6。
  echo         解决办法：删掉 .venv 文件夹后双击 start.bat 重新安装依赖，
  echo                   或在本窗口执行： "%PY%" %PYARG% -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo  正在启动...
"%PY%" %PYARG% main.py
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo  [已退出] 退出码 %CODE% —— 上面的输出就是原因。
  echo           日志：%~dp0data\startup-error.log
  pause
)
exit /b %CODE%
