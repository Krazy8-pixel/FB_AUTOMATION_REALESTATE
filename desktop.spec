from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = Path(SPECPATH)
datas = [
    (str(ROOT / 'web/templates'), 'web/templates'),
    (str(ROOT / 'web/static'), 'web/static'),
    (str(ROOT / 'assets'), 'assets'),
    (str(ROOT / 'app_config.json'), '.'),
    (str(ROOT / 'build/updater/TayyaraUpdater.exe'), '.'),
]
datas += collect_data_files('webview')
datas += collect_data_files('selenium', includes=['webdriver/common/windows/selenium-manager.exe'])
a = Analysis([str(ROOT / 'desktop.py')], pathex=[str(ROOT), str(ROOT / '.desktop_deps'), str(ROOT / '.listing_test_deps')], binaries=[], datas=datas,
             hiddenimports=collect_submodules('webview') + collect_submodules('pystray') + collect_submodules('selenium') + ['apscheduler.jobstores.sqlalchemy'],
             excludes=['pytest', 'tkinter', 'matplotlib', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'IPython', 'notebook'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='TayyaraMotors', console=False, icon=str(ROOT / 'assets/app.ico'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='TayyaraMotors')
