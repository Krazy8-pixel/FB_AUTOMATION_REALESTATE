from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = Path(SPECPATH)
datas = [
    (str(ROOT / 'web/templates'), 'web/templates'),
    (str(ROOT / 'web/static'), 'web/static'),
    (str(ROOT / 'assets/app-logo.png'), 'assets'),
    (str(ROOT / 'app_config.json'), '.'),
]
datas += collect_data_files('webview')
datas += collect_data_files('selenium')

a = Analysis(
    [str(ROOT / 'desktop.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules('webview') + collect_submodules('pystray')
        + collect_submodules('selenium') + ['apscheduler.jobstores.sqlalchemy'],
    excludes=['pytest', 'tkinter', 'matplotlib', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'IPython', 'notebook'],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='FBAutomationRealEstate', console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='FBAutomationRealEstate')
app = BUNDLE(coll, name='FBAutomationRealEstate.app', bundle_identifier='com.tayyara.FBAutomationRealEstate')
