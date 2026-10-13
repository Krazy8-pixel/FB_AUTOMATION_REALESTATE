@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0.domain_checker_deps;%PYTHONPATH%"
set "CHECKER_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%CHECKER_PYTHON%" (
    "%CHECKER_PYTHON%" automation\bbc.py
) else (
    python automation\bbc.py
)
pause
