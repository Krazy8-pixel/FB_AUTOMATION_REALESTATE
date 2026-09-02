param()

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$watchdogBat = Join-Path $projectRoot "tools\watchdog_run.bat"

if (-not (Test-Path $watchdogBat)) {
    throw "Watchdog script not found: $watchdogBat"
}

$startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
if (-not (Test-Path $startupDir)) {
    New-Item -ItemType Directory -Path $startupDir -Force | Out-Null
}

$launcherPath = Join-Path $startupDir "FBAutomationWatchdog.cmd"
$content = @(
    "@echo off",
    "cd /d `"$projectRoot`"",
    "call `"$watchdogBat`""
)
Set-Content -Path $launcherPath -Value $content -Encoding ASCII

Write-Host "Startup launcher installed:"
Write-Host $launcherPath
Write-Host "It will run at user logon."
