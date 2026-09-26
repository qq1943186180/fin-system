# -*- mode: python ; coding: utf-8 -*-
# Canonical entry: project_root / "run_finance.py" (wrapper that sets sys.path)

a = Analysis(
    ['run_finance.py'],
    pathex=[],
    binaries=[],
    datas=[('db', 'db'), ('modules', 'modules'), ('gui', 'gui'), ('icon.ico', '.')],
    hiddenimports=['sqlite3', 'urllib.request', 'urllib.error', 'modules.ledger', 'modules.reports', 'modules.arap', 'modules.expense', 'modules.invoice_gui', 'modules.einvoice', 'modules.feishu', 'modules.feishu_pull', 'modules.mail_service', 'modules.mail_sync', 'modules.mail_processor', 'modules.closing', 'modules.inventory', 'gui.page_mail', 'gui.page_inventory', 'gui.page_closing', 'pystray', 'imap_tools', 'fpdf', 'openpyxl'],
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
