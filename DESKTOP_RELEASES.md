# Desktop app and releases

The client receives `release/FacebookMarketplaceAutomation-Setup-1.0.1.exe`. Setup includes Python and Selenium Manager. The client must install regular Google Chrome. The app detects Chrome and downloads/caches a matching automation driver automatically, including after Chrome updates. It does not download Chrome for Testing. Setup prepares Microsoft WebView2 automatically when needed. Internet access is required for a new driver download and Facebook tasks.

On first launch, the client creates their portal login and adds their Facebook accounts inside the app. Existing developer accounts and databases are never included in the distribution.

## Saved data

Installed program files live in `%LOCALAPPDATA%\Programs\TayyaraMotors`. Client accounts, sessions, groups, listings, media, preferences, and schedules live separately in `%LOCALAPPDATA%\TayyaraMotors\data`.

Ordinary updates replace program files only. Uninstalling retains saved data. Facebook itself can still expire a session or request verification.

## Background operation

Closing the window hides it in the tray. Start With Windows and Background Sync are enabled by default and can be changed in Settings. Exit App in the tray stops the app after current work finishes.

Imports run once every 24 hours and click refreshes every 4 hours for each account. Maintenance runs one task at a time and waits for manual automation. Sleeping or powered-off PCs cannot run tasks. Each overdue maintenance task runs once after the app resumes. Pending schedules are restored; interrupted posting jobs require review to avoid duplicate posts.

## Publish a code update from this PC

Use a higher version for every release. From this project's PowerShell terminal:

```powershell
$releasePython = 'C:/Users/hp/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$env:PYTHONPATH = "$PWD;$PWD\.desktop_deps;$PWD\.listing_test_deps"
& $releasePython tools/build_desktop.py --version 1.0.1
$env:GH_CONFIG_DIR = "$PWD\packaging\github-auth"
$releaseGh = (Get-ChildItem packaging/compiler/github -Recurse -Filter gh.exe | Select-Object -First 1).FullName
& $releasePython tools/publish_desktop.py --version 1.0.1 --gh $releaseGh
```

This publishes four files to `Krazy8-pixel/facebook-marketplace-automation-exe-app`: the setup EXE, update ZIP, manifest, and signature. It uploads neither source code nor client data.

Notify the client, who then uses **Settings → Check for Updates → Download & Install**. The app verifies the signature and checksum, exits, installs the new version, and restarts. If startup fails, it restores the previous program version.

Keep `.release_keys/ed25519.key` securely backed up. Existing installations trust its matching public key. Do not delete, regenerate, publish, or send that private key to clients. The local GitHub authentication folder must also remain private.

For an intentional listing-database reset, add `--reset-listings` to the build command. The client is asked before installing; accounts and groups remain. The old listing database is backed up for rollback. Changes to the developer's own database contents do not automatically replace client data.

## Rebuild on another developer PC

Use Python 3.12 on Windows, install `requirements-desktop.txt`, restore the private signing key, then run `tools/prepare_desktop.py` and `tools/build_desktop.py --version VERSION`. Authenticate GitHub CLI before publishing. Do not include developer data in the bundle.

The Windows installer is not Authenticode-signed. Manifest signatures protect the app's update channel; a commercial Windows signing certificate is a separate optional distribution step.
