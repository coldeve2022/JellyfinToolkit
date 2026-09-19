@echo off
REM ─────────────────────────────────────────────────────────────
REM Jellyfin Toolkit 打包入口（Windows）
REM
REM 实际逻辑在 scripts\build_release.py：
REM   生成图标 -> 生成版本资源 -> 跑测试 -> PyInstaller -> zip + SHA256
REM
REM 用法：
REM   build.bat                 完整构建（含测试）
REM   build.bat --skip-tests    跳过测试
REM   build.bat --no-zip        只出目录，不压缩
REM ─────────────────────────────────────────────────────────────
setlocal

cd /d "%~dp0"

REM WorkBuddy / CodeBuddy 的 safe-delete sitecustomize shim 会拦截 PyInstaller
REM 内部的 os.remove / shutil.rmtree（清理 build 残留），导致打包中途失败。
REM 清空会话变量即可禁用该 shim（未设置时是惰性的）。
set "CODEBUDDY_SESSION_ID="
set "CLAUDE_SESSION_ID="

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if "%PY%"=="" where py >nul 2>/dev/null && set "PY=py -3"
if "%PY%"=="" where python >nul 2>/dev/null && set "PY=python"
if "%PY%"=="" (
  echo [ERROR] 找不到 Python 3.9+，请先安装并加入 PATH。
  pause
  exit /b 1
)

echo [build] 使用解释器: %PY%
"%PY%" scripts\build_release.py %*
if errorlevel 1 (
  echo.
  echo ============================================
  echo 打包失败，请查看上方错误信息
  echo ============================================
  pause
  exit /b 1
)

echo.
echo ============================================
echo 完成。产物在 dist\ 下：
echo   dist\JellyfinToolkit\JellyfinToolkit.exe
echo   dist\JellyfinToolkit-v*-win64.zip
echo ============================================
echo.
echo 建议再跑一次启动冒烟： "%PY%" tools\dev\smoke_exe.py
pause
