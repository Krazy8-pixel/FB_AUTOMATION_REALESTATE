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

Closing the window cancels foreground automation and queued posts/reposts permanently. Manually started group fetching, listing imports, and click refreshes can finish in the tray. Exit App stops the process so program files can be replaced during installation. Start With Windows can be changed in Settings.

Automatic background Facebook maintenance has been removed: no 24-hour listing imports, 4-hour click refreshes, daily cookie checks, or catch-up maintenance after sleep or restart. Saved Background Sync preferences cannot re-enable it. Old recurring maintenance schedules are removed while the scheduler is paused at startup, before pending jobs can execute.

Users can still import listings and groups, refresh clicks, and check sessions manually. User-created post and repost schedules remain available. Pending schedules are restored; interrupted posting jobs require review to avoid duplicate posts. Internal job-state persistence remains enabled.

Scheduled Posts and Repost Status show running actions with
Cancel controls. Queued schedules can be cancelled before they start. Running
cancellation prevents further browser commands and interrupts posting delays;
an action already submitted to Facebook cannot be undone. Updates and uninstall
stop the actual app process; closing the window alone leaves the app in the tray.

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

## Navigation responsiveness

Page requests wait at most half a second for shared task/account metadata locks;
busy requests return a retry message instead of occupying server workers
indefinitely. Listing database lock waits are limited to one second for UI
requests; background transactions retain their existing timeout. Create Listings
uses the restored job registry instead of querying the scheduler to render its
availability. Job-state disk writes no longer hold page-facing registry locks.

UI API calls have a 15-second deadline; timed-out writes are never automatically resubmitted.
A timeout does not cancel an operation already accepted by the server; review
Posting Results before retrying a post. Running-actions polling waits for its
previous response before scheduling another request.

An independent local monitor records Python thread stacks when a request runs
over ten seconds or a UI metadata-lock wait times out. Captures are throttled to
once per minute and saved in `data/freeze-diagnostics.log` with two rotated
backups (about 6 MB total). They are included in Download diagnostic report.
The monitor records request method/path and stack locations, without query
strings, submitted forms, cookies, or frame-local values. It does not run any
Facebook automation or restart the app. Retest the rebuilt desktop app on the
affected PC; these safeguards do not establish the exact cause of a past freeze.

## Version 1.0.27 listing workflow

Manage Listings contains the selected account's Facebook posts. Backup Posts is
its own sidebar page and shows shared backups across all accounts. Save template
stores local details/media and opens Backup Posts without publishing. Confirmed
manual posts are saved under the target account; confirmed Facebook deletions
move to shared backups immediately.

Backup entries are grouped using year, make, model, price, and mileage only.
Any differing or missing comparison field keeps entries separate. Internal
records remain available for existing job references; clearing a grouped entry
removes its matching backup copies. Facebook-posted records stay account scoped.
Clear local listings removes only the selected account's database records.

Bulk actions use selection within their page and inline account/delay controls.
Backup posting can include the active account; Facebook cross posting excludes
it. Bulk account posting schedules successive listings at interval multiples
from one server start time (for example 5, 10, 15 minutes). Due jobs wait for the
posting queue, and Posting Results shows their scheduled times. Existing queued
jobs retain the schedules created before an update.

Import media comes from the exact public listing in a separate tab; other fields
come from its edit page. Images are saved without resizing; optional videos are
saved when a direct HD source is available. Deletion scanning does not inspect
images, uses 25-second element waits, and keeps the final five-second delay.
Posting allows an empty condition and recovers supplied fields cleared by
Facebook only after waiting for the action to render; the final seven-second
Publish delay is retained. Manual posting and reposting use the existing UI
confirmation; account-to-account posting retains its click-and-wait success rule.

Finish active jobs and restart the app after updating Python routes. Refreshing
an already running browser does not register newly added server endpoints.
