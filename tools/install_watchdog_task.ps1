param(
    [string]$TaskName = "FBAutomationWatchdog"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$watchdogBat = Join-Path $projectRoot "tools\watchdog_run.bat"
$taskRunner = "$env:WINDIR\System32\cmd.exe"
$taskArgs = "/c `"$watchdogBat`""

if (-not (Test-Path $watchdogBat)) {
    throw "Watchdog script not found: $watchdogBat"
}

cmd /c "schtasks /Delete /TN `"$TaskName`" /F >nul 2>&1 || exit /b 0" | Out-Null

$createOutput = schtasks /Create `
    /TN $TaskName `
    /TR "`"$taskRunner`" $taskArgs" `
    /SC ONLOGON `
    /RL LIMITED `
    /F 2>&1

if ($LASTEXITCODE -ne 0) {
    throw "Failed to create task. schtasks output: $createOutput"
}

Write-Host "Created task '$TaskName' (ONLOGON)."
Write-Host "Project root: $projectRoot"
Write-Host "Watchdog: $watchdogBat"
Write-Host "To run now: schtasks /Run /TN `"$TaskName`""
