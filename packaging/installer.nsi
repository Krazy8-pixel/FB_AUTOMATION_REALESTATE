!include "MUI2.nsh"
!include "LogicLib.nsh"
Name "Facebook Marketplace Automation"
OutFile "..\release\FacebookMarketplaceAutomation-Setup-${APP_VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\TayyaraMotors"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Icon "..\assets\app.ico"
UninstallIcon "..\assets\app.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\TayyaraMotors.exe"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"
Function .onInit
  System::Call 'kernel32::OpenMutexW(i 0x100000, i 0, w "Local\TayyaraMotorsDesktop") p .r0'
  ${If} $0 != 0
    System::Call 'kernel32::CloseHandle(p r0)'
    MessageBox MB_OK "Please exit the app using its tray menu before installing."
    Abort
  ${EndIf}
FunctionEnd
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
SectionEnd
Function un.CloseApp
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
      MessageBox MB_OK "The app could not close automatically. Exit it from the tray menu, then run uninstall again."
      Abort
    ${EndIf}
  ${EndIf}
FunctionEnd
Section "Uninstall"
  SetShellVarContext current
  Call un.CloseApp
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "TayyaraMotors"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\TayyaraMotors"
  Delete "$DESKTOP\Tayyara Motors.lnk"
  Delete "$SMPROGRAMS\Tayyara Motors\Tayyara Motors.lnk"
  Delete "$SMPROGRAMS\Tayyara Motors\Uninstall Tayyara Motors.lnk"
  RMDir "$SMPROGRAMS\Tayyara Motors"
  ; Only the fixed program directory is removed. Saved client data is retained.
  RMDir /r "$INSTDIR"
SectionEnd
