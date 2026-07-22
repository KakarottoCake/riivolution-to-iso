# PyInstaller spec for the Windows release build.
#
#   pyinstaller packaging/RiivolutionUltimatum.spec --noconfirm
#
# Produces a single windowed .exe. wit is NOT embedded here -- it ships as a
# separate `wit/` folder beside the .exe (see packaging/build_release.ps1), so
# it stays a plainly separate GPL-2 program that we merely invoke.

import os

repo_root = os.path.abspath(os.getcwd())

a = Analysis(
    ["launch.py"],
    pathex=[repo_root],
    binaries=[],
    datas=[],
    hiddenimports=["riivultimatum.gui", "riivultimatum.cli"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Trim the bundle: none of these are imported by the app.
        "numpy", "pandas", "PIL", "pytest", "setuptools", "pip",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RiivolutionUltimatum",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI app: no console window on double-click
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
