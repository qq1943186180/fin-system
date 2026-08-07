# -*- mode: python ; coding: utf-8 -*-
# Canonical entry: project_root / "run_finance.py" (wrapper that sets sys.path)

a = Analysis(
    ['run_finance.py'],
    pathex=[],
    binaries=[],
    datas=[('db', 'db'), ('modules', 'modules')],
    hiddenimports=['sqlite3', 'urllib.request', 'urllib.error', 'db', 'ledger', 'reports', 'arap', 'expense', 'invoice_gui', 'feishu', 'mail_service', 'mail_sync', 'mail_processor', 'page_mail', 'pystray', 'imap_tools'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='财务系统',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='财务系统',
)
