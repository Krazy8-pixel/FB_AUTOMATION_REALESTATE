!include "MUI2.nsh"
!include "LogicLib.nsh"
Name "Facebook Marketplace Automation"
!ifdef GENERATE_UNINSTALLER
OutFile "..\build\uninstall\MakeUninstaller.exe"
!else
!ifdef SETUP_OUTFILE
OutFile "${SETUP_OUTFILE}"
!else
OutFile "..\release\FacebookMarketplaceAutomation-Setup-${APP_VERSION}.exe"
!endif
!endif
InstallDir "$LOCALAPPDATA\Programs\TayyaraMotors"
RequestExecutionLevel user
!ifdef FAST_BUILD
SetCompressor /SOLID zlib
!else
SetCompressor /SOLID lzma
!endif
Icon "..\assets\app.ico"
UninstallIcon "..\assets\app.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\TayyaraMotors.exe"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!define MUI_UNCONFIRMPAGE_TEXT_TOP "Uninstall will remove Tayyara Motors and all saved accounts, sessions, listings, groups, media, settings, and schedules from this computer."
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"
; NSIS is 32-bit: inspect the machine's 32-bit registration and both user views.
Function DetectWebViewRuntime
  StrCpy $1 ""
  SetRegView 32
  ReadRegStr $1 HKLM "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${If} $1 == ""
  ${OrIf} $1 == "0.0.0.0"
    ReadRegStr $1 HKCU "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${EndIf}
  ${If} $1 == ""
  ${OrIf} $1 == "0.0.0.0"
    SetRegView 64
    ReadRegStr $1 HKCU "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${EndIf}
  SetRegView 32
FunctionEnd
Section "Install"
!ifdef GENERATE_UNINSTALLER
  WriteUninstaller "$EXEDIR\Uninstall.exe"
!else
  SetShellVarContext current
  SetOutPath "$TEMP\TayyaraMotorsSetup"
  File "prerequisites\MicrosoftEdgeWebview2Setup.exe"
  DetailPrint "Preparing the desktop web runtime..."
  Call DetectWebViewRuntime
  ${If} $1 == ""
  ${OrIf} $1 == "0.0.0.0"
    ExecWait '"$TEMP\TayyaraMotorsSetup\MicrosoftEdgeWebview2Setup.exe" /silent /install' $0
    Call DetectWebViewRuntime
    ${If} $1 == ""
    ${OrIf} $1 == "0.0.0.0"
      MessageBox MB_OK "Microsoft WebView2 Runtime is unavailable (installer code: $0). Please install Microsoft WebView2 Evergreen Runtime and run setup again."
      Abort
    ${EndIf}
  ${Else}
    DetailPrint "Using installed WebView2 Runtime $1."
  ${EndIf}
  Delete "$TEMP\TayyaraMotorsSetup\MicrosoftEdgeWebview2Setup.exe"
  RMDir "$TEMP\TayyaraMotorsSetup"
  Call CloseApp
  Call CheckAppWritable
  SetOutPath "$INSTDIR"
  File /r "..\dist\TayyaraMotors\*"
  CreateShortcut "$DESKTOP\Tayyara Motors.lnk" "$INSTDIR\TayyaraMotors.exe"
  CreateDirectory "$SMPROGRAMS\Tayyara Motors"
  CreateShortcut "$SMPROGRAMS\Tayyara Motors\Tayyara Motors.lnk" "$INSTDIR\TayyaraMotors.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "TayyaraMotors" '$\"$INSTDIR\TayyaraMotors.exe$\" --background'
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortcut "$SMPROGRAMS\Tayyara Motors\Uninstall Tayyara Motors.lnk" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TayyaraMotors" "DisplayName" "Facebook Marketplace Automation"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TayyaraMotors" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TayyaraMotors" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TayyaraMotors" "DisplayIcon" "$INSTDIR\TayyaraMotors.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TayyaraMotors" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TayyaraMotors" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TayyaraMotors" "NoRepair" 1
!endif
SectionEnd
!macro CloseAppFunction PREFIX
Function ${PREFIX}CloseApp
close_app_retry:
  ; Ask the tray instance to stop, rather than making users find its menu.
  System::Call 'kernel32::OpenMutexW(i 0x100000, i 0, w "Local\TayyaraMotorsDesktop") p .r0'
  ${If} $0 != 0
    System::Call 'kernel32::CloseHandle(p r0)'
    CreateDirectory "$LOCALAPPDATA\TayyaraMotors\data"
    FileOpen $1 "$LOCALAPPDATA\TayyaraMotors\data\exit-for-uninstall" w
    FileClose $1
    StrCpy $2 0
    ${Do}
      Sleep 500
      System::Call 'kernel32::OpenMutexW(i 0x100000, i 0, w "Local\TayyaraMotorsDesktop") p .r0'
      ${If} $0 == 0
        ${ExitDo}
      ${EndIf}
      System::Call 'kernel32::CloseHandle(p r0)'
      IntOp $2 $2 + 1
    ${LoopUntil} $2 >= 60
    Delete "$LOCALAPPDATA\TayyaraMotors\data\exit-for-uninstall"
    ${If} $0 != 0
      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Tayyara Motors is still running. Close it in Task Manager (TayyaraMotors.exe), then click Retry. No program files have been changed." /SD IDCANCEL IDRETRY close_app_retry
      Abort
    ${EndIf}
  ${EndIf}
FunctionEnd
!macroend
!ifndef GENERATE_UNINSTALLER
!insertmacro CloseAppFunction ""
Function CheckAppWritable
check_app_retry:
  IfFileExists "$INSTDIR\TayyaraMotors.exe" 0 check_app_done
  DetailPrint "Waiting for TayyaraMotors.exe to be released..."
  StrCpy $2 0
  ${Do}
    ; OPEN_EXISTING + GENERIC_WRITE + no sharing: test without changing bytes.
    System::Call 'kernel32::CreateFileW(w "$INSTDIR\TayyaraMotors.exe", i 0x40000000, i 0, p 0, i 3, i 0, p 0) p .r0'
    ${If} $0 != -1
      System::Call 'kernel32::CloseHandle(p r0)'
      Goto check_app_done
    ${EndIf}
    Sleep 500
    IntOp $2 $2 + 1
  ${LoopUntil} $2 >= 60
  MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Windows cannot write to $INSTDIR\TayyaraMotors.exe. Close TayyaraMotors.exe in Task Manager and check the folder permissions, then click Retry. If it remains locked, restart Windows before installing. No program files have been changed." /SD IDCANCEL IDRETRY check_app_retry
  Abort
check_app_done:
FunctionEnd
!endif
!insertmacro CloseAppFunction "un."
Section "Uninstall"
  SetShellVarContext current
  Call un.CloseApp
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "TayyaraMotors"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TayyaraMotors"
  Delete "$DESKTOP\Tayyara Motors.lnk"
  Delete "$SMPROGRAMS\Tayyara Motors\Tayyara Motors.lnk"
  Delete "$SMPROGRAMS\Tayyara Motors\Uninstall Tayyara Motors.lnk"
  RMDir "$SMPROGRAMS\Tayyara Motors"
  ; Delete this app's saved accounts, sessions, media, databases, and caches.
  RMDir /r "$LOCALAPPDATA\TayyaraMotors"
  RMDir /r "$INSTDIR"
SectionEnd
