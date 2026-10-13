"""Publish only installers and signed updates; never upload workspace data."""
import argparse
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', required=True)
    parser.add_argument('--gh', default=shutil.which('gh'))
    args = parser.parse_args()
    if not args.gh:
        parser.error('Install GitHub CLI and run gh auth login first, or pass --gh.')
    from packaging.version import Version
    Version(args.version)
    release = ROOT / 'release'
    files = [release / f'FacebookMarketplaceAutomation-Setup-{args.version}.exe',
             release / f'FacebookMarketplaceAutomation-{args.version}.zip',
             release / 'manifest.json', release / 'manifest.sig']
    if not all(path.is_file() for path in files):
        parser.error('Build all four release files before publishing.')
    import json
    manifest = json.loads((release / 'manifest.json').read_text())
    if manifest['version'] != args.version:
        parser.error('The manifest version does not match the requested release.')
    from automation.release_security import verify_manifest
    config = json.loads((ROOT / 'app_config.json').read_text())
    verify_manifest((release / 'manifest.json').read_bytes(), (release / 'manifest.sig').read_bytes(), config['update_public_key'])
    import hashlib
    with files[1].open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['sha256']:
            parser.error('The update archive checksum does not match the manifest.')
    notes = release / 'release-notes.md'
    notes.write_text('Download **FacebookMarketplaceAutomation-Setup-' + args.version + '.exe** for a new Windows installation.\n\nExisting installations can check for this release in **Settings → Check for Updates**. The ZIP and manifest files are used by the app updater.\n\n' + ('This release resets saved listings; accounts and groups are kept.' if manifest['reset_databases'] else 'This release preserves saved app data.'), encoding='utf-8')
    repository = 'Krazy8-pixel/facebook-marketplace-automation-exe-app'
    existing = subprocess.run([args.gh, 'release', 'view', 'v' + args.version, '--repo', repository, '--json', 'isDraft'], capture_output=True, text=True)
    if existing.returncode == 0:
        if not json.loads(existing.stdout)['isDraft']:
            parser.error('This version is already public. Build a higher version instead.')
        releases = json.loads(subprocess.check_output([args.gh, 'api', 'repos/' + repository + '/releases?per_page=100'], text=True))
        remote = next(item for item in releases if item['tag_name'] == 'v' + args.version and item['draft'])
        uploaded = {item['name']: item for item in remote['assets']}
        remaining = []
        for path in files:
            with path.open('rb') as stream:
                digest = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
            asset = uploaded.get(path.name, {})
            if asset.get('digest') == digest and asset.get('size') == path.stat().st_size and asset.get('state') == 'uploaded':
                print('Already verified on GitHub:', path.name, flush=True)
            else:
                remaining.append(path)
    else:
        subprocess.run([args.gh, 'release', 'create', 'v' + args.version, '--repo', repository, '--title', 'Facebook Marketplace Automation EXE App ' + args.version, '--notes-file', str(notes), '--draft'], check=True)
        remaining = files
    # Keep completed uploads when a later file hits a network interruption.
    for path in remaining:
        print('Uploading:', path.name, flush=True)
        subprocess.run([args.gh, 'release', 'upload', 'v' + args.version, str(path), '--repo', repository, '--clobber'], check=True)
    # Clients see the release only after every update asset is uploaded.
    subprocess.run([args.gh, 'release', 'edit', 'v' + args.version, '--repo', repository, '--draft=false', '--latest'], check=True)


if __name__ == '__main__':
    main()
