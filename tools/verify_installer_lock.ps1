$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$testRoot = Join-Path $taskRoot 'outputs\installer-lock-test'
New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
$installerSource = Get-Content -LiteralPath (Join-Path $taskRoot 'packaging\installer.nsi') -Raw
$function = [regex]::Match($installerSource, '(?s)Function CheckAppWritable\r?\n.*?FunctionEnd').Value
if (-not $function) { throw 'Installer lock check not found.' }
# Exercise the real preflight with a shorter timeout, without installing anything.
$function = $function.Replace('${LoopUntil} $2 >= 60', '${LoopUntil} $2 >= 2')
$harness = @'
!include "LogicLib.nsh"
Name "Installer lock test"
OutFile "lock-test.exe"
RequestExecutionLevel user
SilentInstall silent
Section
  StrCpy $INSTDIR "$EXEDIR"
  SetErrorLevel 7
  Call CheckAppWritable
  SetErrorLevel 0
SectionEnd
'@ + "`n" + $function
$script = Join-Path $testRoot 'lock-test.nsi'
[IO.File]::WriteAllText($script, $harness)
$compiler = Join-Path $taskRoot 'packaging\compiler\nsis-3.12\makensis.exe'
& $compiler /V2 $script
if ($LASTEXITCODE -ne 0) { throw 'Test harness compilation failed.' }
$target = Join-Path $testRoot 'TayyaraMotors.exe'
[IO.File]::WriteAllText($target, 'Installer lock test: preserve these bytes.')
$runner = Join-Path $testRoot 'lock-test.exe'
$result = Start-Process -FilePath $runner -ArgumentList '/S' -WindowStyle Hidden -Wait -PassThru
if ($result.ExitCode -ne 0) { throw 'Writable executable was rejected.' }
$lock = [IO.File]::Open($target, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try {
    $result = Start-Process -FilePath $runner -ArgumentList '/S' -WindowStyle Hidden -Wait -PassThru
    if ($result.ExitCode -eq 0) { throw 'Locked executable was accepted.' }
} finally {
    $lock.Dispose()
}
if ([IO.File]::ReadAllText($target) -ne 'Installer lock test: preserve these bytes.') {
    throw 'The preflight changed the existing executable.'
}
Write-Output 'PASS: writable file accepted; locked file blocked; original bytes preserved.'
