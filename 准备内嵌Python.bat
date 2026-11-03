@echo off
REM Prepare the bundled Python runtime under vendor\python (needed by packaging).
REM This file must stay pure ASCII: cmd.exe on a Chinese Windows reads .bat in the
REM system ANSI code page (936/GBK), and UTF-8 Chinese bytes can break line parsing.
setlocal
cd /d "%~dp0"

echo.
echo   Preparing bundled Python for packaging...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\prepare-python.ps1"
if errorlevel 1 (
    echo.
    echo   [FAILED] See the messages above.
    pause
    exit /b 1
)

echo.
echo   Done. You can run the packaging script now.
pause