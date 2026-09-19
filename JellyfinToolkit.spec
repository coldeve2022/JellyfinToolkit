# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir，GUI 无控制台）。

要点：
- ``upx=False``：UPX 压缩过的 exe 极易被国产安全软件误报，省那点体积不值当。
- ``console=False``：GUI 应用，异常由 main.py 的 sys.excepthook 落盘到
  <数据目录>/app-error.log（打包后没有控制台，只能靠日志）。
- ``version`` / ``icon``：由 version.py 与 tools/make_icon.py 生成，
  保证右键「属性」看得到版本、任务栏看到的是自己的图标。
- onedir 优于 onefile：Qt 应用 onefile 每次启动都要解压，慢得多。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)  # noqa: F821 — PyInstaller 注入

# 本工程使用到的自带模块（必须显式收集，避免被 optimize 掉）
hidden = []
for pkg in ("utils", "workers", "ui", "automation"):
    hidden += collect_submodules(pkg)

# 明确没用到、且体积很大的库 —— 剔掉能显著缩小体积与启动时间
excludes = [
    "tkinter", "turtle", "idlelib", "lib2to3",
    "numpy", "scipy", "pandas", "matplotlib", "PIL", "pillow",
    "IPython", "jupyter", "notebook", "pytest", "pytest_qt",
    "pygame", "cv2", "torch", "tensorflow",
    # Qt 里没用到的重型模块
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSql",
    "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSerialPort", "PySide6.QtSensors", "PySide6.QtSpatialAudio",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech", "PySide6.QtHelp", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtNetworkAuth", "PySide6.QtHttpServer",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JellyfinToolkit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "icon.ico"),
    version=str(ROOT / "build" / "version_info.txt"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="JellyfinToolkit",
)
