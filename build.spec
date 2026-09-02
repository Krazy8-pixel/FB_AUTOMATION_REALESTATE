# build.spec
import os
from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.getcwd())
asset_datas = []
assets_dir = os.path.join(ROOT, "assets")
if os.path.isdir(assets_dir):
    asset_datas.append((assets_dir, "assets"))

hiddenimports = collect_submodules("selenium")

a = Analysis(
    [os.path.join(ROOT, "run.py")],
    pathex=[ROOT],
    binaries=[(os.path.join(ROOT, "drivers", "chromedriver.exe"), "drivers")],
    datas=[
        (os.path.join(ROOT, "web", "templates"), "web/templates"),
        (os.path.join(ROOT, "web", "static"), "web/static"),
        *asset_datas,
    ],
    hiddenimports=hiddenimports,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FBAutomation",
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="FBAutomation",
)

