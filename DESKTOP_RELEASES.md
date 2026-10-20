# Desktop app and releases

The client receives `release/FacebookMarketplaceAutomation-Setup-1.0.1.exe`. Setup includes Python and Selenium Manager. The client must install regular Google Chrome. The app detects Chrome and downloads/caches a matching automation driver automatically, including after Chrome updates. It does not download Chrome for Testing. Setup prepares Microsoft WebView2 automatically when needed. Internet access is required for a new driver download and Facebook tasks.

On first launch, the client creates their portal login and adds their Facebook accounts inside the app. Existing developer accounts and databases are never included in the distribution.

## Saved data

Updates are manual: use Check for Updates and Download & Install in Settings.
Installation verifies the signed manifest and archive checksum. Finish running
automation before installing an update.

Installed program files live in `%LOCALAPPDATA%\Programs\TayyaraMotors`. Client accounts, sessions, groups, listings, media, preferences, and schedules live separately in `%LOCALAPPDATA%\TayyaraMotors\data`.

Ordinary updates replace program files only. From version 1.0.11, uninstalling also deletes saved accounts, sessions, listings, groups, media, preferences, schedules, and caches. Facebook itself can still expire a session or request verification.

## Background operation

Closing the window cancels foreground automation and queued posts/reposts permanently. Manually started group fetching, listing imports, click refresh, and daily cookie checks continue in the tray. Exit App stops the process, including background maintenance, so program files can be replaced during installation. Start With Windows and Background Sync are enabled by default and can be changed in Settings.

Imports run once every 24 hours and click refreshes every 4 hours for each account. Maintenance runs one task at a time and waits for manual automation. Sleeping or powered-off PCs cannot run tasks. Each overdue maintenance task runs once after the app resumes. Pending schedules are restored; interrupted posting jobs require review to avoid duplicate posts.

Groups are imported only when the user starts Import Groups. Startup, adding an
account, and background maintenance do not fetch groups or retry failed group
imports automatically.

Scheduled Posts and Repost Status show running actions with
Cancel controls. Queued schedules can be cancelled before they start. Running
cancellation prevents further browser commands and interrupts posting delays;
an action already submitted to Facebook cannot be undone. Updates and uninstall
stop the actual app process; closing the window alone retains maintenance.

## Publish a code update from this PC

Use a higher version for every release. From this project's PowerShell terminal:

For quicker local builds, add `--fast` to `tools/build_desktop.py`. This reuses
the dependency cache and uses faster ZIP/installer compression; downloads are
larger, but contain the same application and signed update verification.

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

Setup requests the running app to shut down and waits for the executable to become writable before extracting program files. If Windows keeps the file locked, setup offers Retry or Cancel before replacing any program files. Installation preserves saved app data.

In-app updates launch an independent updater from the data directory without
inheriting file handles or the app's DLL search path. The updater waits for app
shutdown, backs up and replaces files individually with sharing-violation retries,
and restores the backup if installation or startup fails. The new executable is
installed last, after its libraries. Existing installations must first receive
this updater through a rebuilt installer; older versions still run their bundled
updater when starting an update.

## Optional listing video backups

Run Import Listings again to recover videos for existing saved listings. The
normal edit-page reader checks HTML video/currentSrc/source URLs, independently
of OCR. Direct HTTPS Facebook CDN videos with MP4, WebM, or QuickTime content
types are downloaded without recompression (up to five, 150 MB each, with a
60-second download deadline per video). Blob and streaming-only sources are
skipped; this does not claim every Facebook video can be recovered.

Saved videos play on the listing detail page. Reposts and cross-posts submit
existing local video files using the optional video uploader. Cross-post target
backups receive their own copies. Missing files, unsupported upload controls,
and video download/upload failures do not make videos required for posting.
Previously saved video files are retained if a refresh cannot recover a video.
Source changes require a new desktop build before installed EXEs include them.

## Remote test diagnostic report

After installing a build with diagnostics, reproduce one workflow at a time:
Import Listings, post to another account, then repost only a disposable listing.
Reposting deletes the original first. Note the account, listing title, and test
time, then use Settings > Download diagnostic report. This downloads
`tayyara-diagnostics.txt` with recent output and CHECK markers. The Live Terminal
also offers this download; its original Download logs button exports only the
visible terminal text.

The installed app saves print output and stderr in
`%LOCALAPPDATA%\TayyaraMotors\data\diagnostics.log`, with two rotated backups
(`.1`, `.2`), up to about 15 MB total. Logs survive restarts, and logging failures
do not interrupt automation. No program-folder write permission is needed.
Reports can contain account names, listing details, paths, and media URLs;
review before sharing. Cookie/session databases are not included by the exporter.

IMPORT_LISTING markers list missing/uncertain fields and media counts.
POST_MEDIA lists photo uploads and optional video submissions. PUBLISH records
CLICKED separately from CONFIRMED_BY_UI or CLICK_ONLY_REVIEW_ON_FACEBOOK.
CROSS_POST names the target account and reports each job result. A FINISHED
marker describes function completion, not proof of publication or complete
extraction. Verify final fields and media on Facebook as well.
