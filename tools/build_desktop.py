"""Build the distributable app, one-file installer, and signed update assets."""
import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import zipfile
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    subprocess.run(args, cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', required=True)
    parser.add_argument('--reset-listings', action='store_true')
    parser.add_argument('--fast', action='store_true', help='Use faster compression for local releases (larger downloads).')
    parser.add_argument('--reuse-updater', action='store_true', help='Keep the existing updater binary for an app-only release.')
    args = parser.parse_args()
    os.chdir(ROOT)
    from packaging.version import Version
    Version(args.version)
    cfg_path = ROOT / 'app_config.json'
    cfg = json.loads(cfg_path.read_text())
    cfg['version'] = args.version
    cfg_path.write_text(json.dumps(cfg, indent=2) + '\n')
    compiler = next((ROOT / 'packaging/compiler').rglob('makensis.exe'))
    (ROOT / 'build/uninstall').mkdir(parents=True, exist_ok=True)
    run(str(compiler), '/DGENERATE_UNINSTALLER', '/DAPP_VERSION=' + args.version, str(ROOT / 'packaging/installer.nsi'))
    run(str(ROOT / 'build/uninstall/MakeUninstaller.exe'), '/S')
    if args.reuse_updater:
        if not (ROOT / 'build/updater/RealEstateUpdater.exe').is_file():
            raise RuntimeError('No existing updater binary to reuse.')
    else:
        run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--noconsole', '--name', 'RealEstateUpdater', '--distpath', 'build/updater', '--workpath', 'build/updater-work', '--specpath', 'packaging', 'updater.py')
    # PyInstaller can reuse the EXE when only the icon file contents change.
    # Rebuild its resource container while retaining the dependency cache.
    (ROOT / 'build/desktop/EXE-00.toc').unlink(missing_ok=True)
    run(sys.executable, '-m', 'PyInstaller', '--noconfirm', 'desktop.spec')
    shutil.copy2(ROOT / 'build/uninstall/Uninstall.exe', ROOT / 'dist/FBAutomationRealEstate/Uninstall.exe')
    # Selenium exposes browser drivers through lazy imports, which PyInstaller
    # cannot discover from the app's webdriver.Chrome call alone.
    from PyInstaller.archive.readers import ZlibArchiveReader
    from PyInstaller.utils.hooks import collect_submodules
    bundled = ZlibArchiveReader(str(ROOT / 'build/desktop/PYZ-00.pyz')).toc
    selenium_modules = set(collect_submodules('selenium'))
    if not selenium_modules:
        raise RuntimeError('Selenium was not discoverable in the desktop build environment.')
    missing = selenium_modules - set(bundled)
    # This is imported dynamically by webdriver.Chrome, so a build with no
    # discoverable Selenium package could otherwise pass an empty-set check.
    missing.update({'selenium.webdriver.chrome.webdriver'} - set(bundled))
    if missing:
        raise RuntimeError('Selenium modules missing from desktop build: ' + ', '.join(sorted(missing)))
    release = ROOT / 'release'
    release.mkdir(exist_ok=True)
    asset = f'FBAutomationRealEstate-{args.version}.zip'
    package = release / asset
    compression = {'compression': zipfile.ZIP_DEFLATED, 'compresslevel': 1} if args.fast else {'compression': zipfile.ZIP_LZMA}
    with zipfile.ZipFile(package, 'w', **compression) as archive:
        for path in (ROOT / 'dist/FBAutomationRealEstate').rglob('*'):
            if path.is_file():
                archive.write(path, path.relative_to(ROOT / 'dist/FBAutomationRealEstate'))
    with package.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    manifest = {'app': 'FBAutomationRealEstate', 'version': args.version, 'asset': asset, 'sha256': checksum, 'reset_databases': ['listings'] if args.reset_listings else []}
    raw = json.dumps(manifest, sort_keys=True).encode()
    (release / 'manifest.json').write_bytes(raw)
    key = Ed25519PrivateKey.from_private_bytes((ROOT / '.release_keys/realestate-ed25519.key').read_bytes())
    (release / 'manifest.sig').write_bytes(key.sign(raw))
    compiler = next((ROOT / 'packaging/compiler').rglob('makensis.exe'))
    options = ['/DFAST_BUILD'] if args.fast else []
    run(str(compiler), *options, '/DAPP_VERSION=' + args.version, str(ROOT / 'packaging/installer.nsi'))
    print('Release files ready in', release)


if __name__ == '__main__':
    main()
