# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files

icon_file = 'assets/icon.icns' if sys.platform == 'darwin' else 'assets/icon.ico'

datas = [('ui/dist', 'ui/dist'), ('VERSION', '.')]
datas += collect_data_files('playwright_stealth')

# pywebview picks its backend with importlib at runtime, so PyInstaller's static
# analysis never sees webview.platforms.edgechromium, and it ships the JS it
# injects as data files. Without both, the frozen app silently falls back to the
# ancient MSHTML engine and opens a blank white window.
binaries = []
hiddenimports = [
    'keyring.backends.Windows',
    'keyring.backends.macOS',
    'keyring.backends.SecretService',
    'keyring.backends.chainer',
    'keyring.backends.fail',
    'webview.platforms.edgechromium',
    'webview.platforms.winforms',
    'webview.platforms.cocoa',
    'webview.platforms.gtk',
    'webview.platforms.qt',
]
for package in ('webview', 'clr_loader', 'pythonnet', 'keyring'):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception:
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# Bundle the Playwright browsers so the app works on a machine without Chrome.
# bot.py looks for them under sys._MEIPASS/pw-browsers. On macOS they are copied
# next to the .app by the release workflow instead, because a .app bundle cannot
# execute a browser from the read-only extraction folder.
if sys.platform != 'darwin' and os.path.isdir('pw-browsers'):
    datas += [('pw-browsers', 'pw-browsers')]


a = Analysis(
    ['server/app.py'],
    pathex=['server'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
