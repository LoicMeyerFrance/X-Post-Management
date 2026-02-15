# -*- mode: python ; coding: utf-8 -*-
import sys
from PyInstaller.utils.hooks import collect_data_files

icon_file = 'assets/icon.icns' if sys.platform == 'darwin' else 'assets/icon.ico'

datas = [('ui/dist', 'ui/dist')]
datas += collect_data_files('playwright_stealth')


a = Analysis(
    ['server/app.py'],
    pathex=['server'],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == 'darwin':
    # macOS: create a .app bundle (double-click to launch, icon in Dock)
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='X Post Management',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=[icon_file],
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        name='X Post Management',
    )
    app = BUNDLE(
        coll,
        name='X Post Management.app',
        icon=icon_file,
        bundle_identifier='com.xpostmanagement.app',
    )
else:
    # Windows: single .exe file
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='X Post Management',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=[icon_file],
    )
