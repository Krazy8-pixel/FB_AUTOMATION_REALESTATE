@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0.."

set "LOG_DIR=%CD%\data"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\watchdog.log"

set "FB_AUTOMATION_NO_BROWSER=1"
set "PY_EXE=python"
if exist "%CD%\.venv\Scripts\python.exe" set "PY_EXE=%CD%\.venv\Scripts\python.exe"

echo [%date% %time%] Watchdog started in %CD%>>"%LOG_FILE%"
echo [%date% %time%] Using Python: %PY_EXE%>>"%LOG_FILE%"

:loop
echo [%date% %time%] Launching app...>>"%LOG_FILE%"
"%PY_EXE%" run.py >>"%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] App exited with code !EXIT_CODE!. Restarting in 5s...>>"%LOG_FILE%"
timeout /t 5 /nobreak >nul
goto loop
