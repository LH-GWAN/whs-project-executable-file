# -*- mode: python ; coding: utf-8 -*-
import os

# datas의 상대 경로는 spec 폴더 기준이지만 glob은 CWD 기준이라 다른 폴더에서 빌드하면 지도·키가
# 조용히 빠졌다(리뷰 #125). 모든 탐색을 spec 폴더(SPECPATH) 기준 절대 경로로 한다.
_ROOT = globals().get("SPECPATH") or os.path.dirname(os.path.abspath(__file__ if "__file__" in globals() else "idas.spec"))

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

vendor_datas = [
    (os.path.join("engine", "vendor", name), os.path.join("engine", "vendor"))
    for name in (
        "integration_blackbox.py",
        "integration_avi.py",
        "integration_mp4.py",
        "ENGINE_README.md",
        "ENGINE_ARCHITECT.md",
        "ENGINE_COMMIT.txt",
        "README_VENDOR.md",
        "LICENSE",
    )
]

web_datas = [
    (os.path.join("ui", "web", "map.html"), os.path.join("ui", "web")),
    (os.path.join("ui", "web", "map_kakao.html"), os.path.join("ui", "web")),
    (os.path.join("ui", "vendor", "maplibre-gl.js"), os.path.join("ui", "vendor")),
    (os.path.join("ui", "vendor", "maplibre-gl.css"), os.path.join("ui", "vendor")),
    (os.path.join("ui", "vendor", "pmtiles.js"), os.path.join("ui", "vendor")),
]

import glob as _glob
_ASSETS = os.path.join(_ROOT, "assets")
for _bm in _glob.glob(os.path.join(_ASSETS, "*.pmtiles")):
    web_datas.append((_bm, "assets"))
if not _glob.glob(os.path.join(_ASSETS, "*.pmtiles")) and _glob.glob(os.path.join(_ASSETS, "*.zip")):
    # build_windows.bat가 먼저 풀어 주지만, spec만 따로 돌릴 때를 위해 알린다(리뷰 #59).
    print("[idas.spec] WARNING: assets/ has a .zip but no .pmtiles - unpack it first "
          "(core.basemap.extract_zipped_basemap) or the exe ships without a basemap")

# 온라인 지도(카카오맵) 키. git에는 없고(.gitignore) 빌드하는 PC의 assets/에 있을 때만
# 번들에 들어간다. 없으면 온라인 모드를 골라도 오프라인 지도로 표시된다.
# 앱(core/appconfig.scan_key_files)은 assets/ 의 .json/.txt 를 이름과 상관없이 키 파일 후보로
# 읽으므로 같은 규칙으로 번들한다(online_keys (1).json 처럼 이름이 어긋난 파일도 포함, 리뷰 #19).
for _pattern in ("*.json", "*.txt"):
    for _keys in _glob.glob(os.path.join(_ASSETS, _pattern)):
        if os.path.basename(_keys).lower() != "readme.txt":
            web_datas.append((_keys, "assets"))

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=vendor_datas + web_datas,
    hiddenimports=[
        "engine_entry",
        "engine.registry",
        "core.paths",
        "argparse",
        "csv",
        "dataclasses",
        "datetime",
        "math",
        "mmap",
        "re",
        "shlex",
        "struct",
        "typing",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "PySide6.QtQuick3D",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DRender",
        "PySide6.QtBluetooth",
        "PySide6.QtNfc",
        "PySide6.QtSerialPort",
        "PySide6.QtTest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="IDAS",
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="IDAS",
)
